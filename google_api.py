"""Google sign-in ("Connect Gmail") and the user's Gmail + Calendar access."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

# What the user sees on Google's "Allow" screen. None of these read their inbox.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",  # send emails on their behalf
    "https://www.googleapis.com/auth/calendar.events",  # create the event once a date is agreed
    "https://www.googleapis.com/auth/calendar.freebusy",  # check the organizer's own conflicts
    # Optional: guest autocomplete. If unticked, the guest box just takes typed addresses.
    "https://www.googleapis.com/auth/contacts.readonly",  # saved contacts
    "https://www.googleapis.com/auth/contacts.other.readonly",  # people auto-saved from past emails
]
REQUIRED_SCOPES = {s for s in SCOPES if "gmail" in s or "calendar" in s}


class MissingPermissions(Exception):
    pass


def _flow(state: str | None = None) -> Flow:
    if not config.GOOGLE_CLIENT_ID or not config.GOOGLE_CLIENT_SECRET:
        raise RuntimeError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (see README).")
    return Flow.from_client_config(
        {
            "web": {
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.OAUTH_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        state=state,
        redirect_uri=config.OAUTH_REDIRECT_URI,
    )


def start_sign_in() -> tuple[str, str, str]:
    """Returns (Google URL to send the user to, state, PKCE code verifier)."""
    flow = _flow()
    url, state = flow.authorization_url(
        access_type="offline",  # get a refresh token so the agent can work while they're away
        prompt="select_account consent",  # always show the account picker and the "Allow" screen
        include_granted_scopes="true",
    )
    return url, state, flow.code_verifier


def finish_sign_in(callback_url: str, state: str, code_verifier: str) -> tuple[dict, str]:
    """Exchange Google's callback for tokens. Returns (user info, token JSON to store)."""
    flow = _flow(state)
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=callback_url)
    creds = flow.credentials

    # Google lets people untick individual permissions on the consent screen.
    granted = set(creds.granted_scopes or [])
    missing = REQUIRED_SCOPES - granted
    if missing:
        raise MissingPermissions(sorted(missing))

    info = id_token.verify_oauth2_token(creds.id_token, Request(), config.GOOGLE_CLIENT_ID)
    user = {"id": info["sub"], "email": info["email"].lower(), "name": info.get("name", "")}
    return user, creds.to_json()


class GoogleAccount:
    """One user's Gmail (send), Calendar and Contacts, built from their stored token."""

    def __init__(self, token_json: str):
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        self.gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.people = build("people", "v1", credentials=creds, cache_discovery=False)

    # ---------------- Contacts (guest autocomplete) ----------------
    # Google's People API has no "most emailed" ranking; that would need reading Gmail
    # headers (a restricted permission). We search saved + auto-saved contacts instead,
    # and suggest starred contacts first, then recently updated ones.

    def warm_up_contacts(self) -> None:
        """Google asks for an empty search first so its search cache is fresh."""
        self.people.people().searchContacts(query="", readMask="names").execute()
        self.people.otherContacts().search(query="", readMask="names").execute()

    def search_contacts(self, query: str, limit: int = 8) -> list[dict]:
        results = []
        saved = self.people.people().searchContacts(
            query=query, readMask="names,emailAddresses", pageSize=limit).execute()
        results += [r["person"] for r in saved.get("results", [])]
        try:
            other = self.people.otherContacts().search(
                query=query, readMask="names,emailAddresses", pageSize=limit).execute()
            results += [r["person"] for r in other.get("results", [])]
        except HttpError:
            pass  # "other contacts" permission may have been unticked
        return _people_to_guests(results)[:limit]

    def suggested_contacts(self, limit: int = 6) -> list[dict]:
        people: list[dict] = []
        try:
            starred = self.people.contactGroups().get(
                resourceName="contactGroups/starred", maxMembers=limit).execute()
            names = starred.get("memberResourceNames", [])
            if names:
                batch = self.people.people().getBatchGet(
                    resourceNames=names, personFields="names,emailAddresses").execute()
                people += [r["person"] for r in batch.get("responses", []) if "person" in r]
        except HttpError:
            pass
        recent = self.people.people().connections().list(
            resourceName="people/me", personFields="names,emailAddresses",
            sortOrder="LAST_MODIFIED_DESCENDING", pageSize=50).execute()
        people += recent.get("connections", [])
        return _people_to_guests(people)[:limit]

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str = "",
    ) -> tuple[str, str]:
        """Send from the user's Gmail. Returns (message id, thread id)."""
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = f"{references} {in_reply_to}".strip()
        msg.set_content(body)
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        sent = self.gmail.users().messages().send(userId="me", body=payload).execute()
        return sent["id"], sent["threadId"]

    def busy_by_date(self, start: str, end: str, tz_name: str) -> dict[str, list[str]]:
        """The user's busy blocks per local date: {"2026-12-03": ["18:00-20:00"]}."""
        tz = ZoneInfo(tz_name)
        time_min = datetime.combine(date.fromisoformat(start), datetime.min.time(), tz)
        time_max = datetime.combine(date.fromisoformat(end) + timedelta(days=1), datetime.min.time(), tz)
        resp = (
            self.calendar.freebusy()
            .query(
                body={
                    "timeMin": time_min.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "timeZone": tz_name,
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )
        out: dict[str, list[str]] = {}
        for block in resp["calendars"]["primary"].get("busy", []):
            s = datetime.fromisoformat(block["start"].replace("Z", "+00:00")).astimezone(tz)
            e = datetime.fromisoformat(block["end"].replace("Z", "+00:00")).astimezone(tz)
            day = s.date()
            while day <= (e - timedelta(seconds=1)).date():
                seg_start = max(s, datetime.combine(day, datetime.min.time(), tz))
                seg_end = min(e, datetime.combine(day + timedelta(days=1), datetime.min.time(), tz))
                end_label = "24:00" if seg_end.date() > day else f"{seg_end:%H:%M}"
                out.setdefault(day.isoformat(), []).append(f"{seg_start:%H:%M}-{end_label}")
                day += timedelta(days=1)
        return out

    @staticmethod
    def _when(day: str, tz_name: str, start_time: str | None, duration_minutes: int) -> dict:
        if not start_time:
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            return {"start": {"date": day}, "end": {"date": next_day}}  # all-day; end is exclusive
        start = datetime.combine(date.fromisoformat(day), datetime.strptime(start_time, "%H:%M").time(),
                                 ZoneInfo(tz_name))
        end = start + timedelta(minutes=duration_minutes)
        return {
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        }

    def create_event(
        self,
        title: str,
        day: str,
        tz_name: str,
        attendees: list[str],
        description: str = "",
        location: str = "",
        start_time: str | None = None,
        duration_minutes: int = 120,
    ) -> tuple[str, str]:
        """Create the event on the user's calendar and email Google invites. Returns (id, link)."""
        created = (
            self.calendar.events()
            .insert(
                calendarId="primary",
                sendUpdates="all",
                body={
                    "summary": title,
                    "description": description,
                    "location": location,
                    "attendees": [{"email": a} for a in attendees],
                    **self._when(day, tz_name, start_time, duration_minutes),
                },
            )
            .execute()
        )
        return created["id"], created.get("htmlLink", "")

    def move_event(
        self, event_id: str, day: str, tz_name: str, attendees: list[str],
        start_time: str | None = None, duration_minutes: int = 120,
    ) -> str:
        """Move an existing event to a new date; Google emails everyone the update. Returns its link."""
        updated = (
            self.calendar.events()
            .patch(
                calendarId="primary",
                eventId=event_id,
                sendUpdates="all",
                body={"attendees": [{"email": a} for a in attendees],
                      **self._when(day, tz_name, start_time, duration_minutes)},
            )
            .execute()
        )
        return updated.get("htmlLink", "")

    def delete_event(self, event_id: str, notify: bool = False) -> None:
        self.calendar.events().delete(
            calendarId="primary", eventId=event_id, sendUpdates="all" if notify else "none"
        ).execute()


def _people_to_guests(people: list[dict]) -> list[dict]:
    """People API persons -> [{"name", "email"}], one per email address, no duplicates."""
    out, seen = [], set()
    for person in people:
        name = (person.get("names") or [{}])[0].get("displayName", "")
        for e in person.get("emailAddresses", [])[:1]:
            email = e.get("value", "").strip().lower()
            if email and email not in seen:
                seen.add(email)
                out.append({"name": name, "email": email})
    return out


def revoke(token_json: str) -> None:
    """Withdraw this app's access to the user's Google account (same as removing it in their
    Google Account settings)."""
    info = json.loads(token_json)
    token = info.get("refresh_token") or info.get("token")
    if not token:
        return
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/revoke",
        data=urllib.parse.urlencode({"token": token}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code != 400:  # 400 = already revoked or expired: nothing left to revoke
            raise
