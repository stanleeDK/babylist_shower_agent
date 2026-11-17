"""Guest replies.

Each invitation has Reply-To: reply+<token>@REPLY_DOMAIN. In production, SendGrid
Inbound Parse receives the reply and POSTs it to /inbound/sendgrid. In demo mode,
simulated guests build the very same form and hand it to `receive`. Either way the
reply lands in the event's inbox and the agent is woken.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.parser import HeaderParser
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html import unescape

import config
import db

MAX_BODY_CHARS = 4000
_LOCAL_PART = re.compile(r"^reply\+([a-f0-9]+)$")


def reply_address(token: str) -> str:
    return f"reply+{token}@{config.REPLY_DOMAIN}"


def _token_from(addr: str) -> str | None:
    local, _, domain = addr.strip().lower().rpartition("@")
    m = _LOCAL_PART.match(local)
    return m.group(1) if m and domain == config.REPLY_DOMAIN else None


def parse_sendgrid(form: dict[str, str]) -> dict | None:
    """Inbound Parse fields -> reply dict, or None if it isn't addressed to us."""
    headers = HeaderParser().parsestr(form.get("headers", ""))
    recipients: list[str] = []
    try:
        recipients += json.loads(form.get("envelope") or "{}").get("to", [])
    except json.JSONDecodeError:
        pass
    recipients += [a for _, a in getaddresses([form.get("to", ""), form.get("cc", "")])]
    token = next((t for t in map(_token_from, recipients) if t), None)
    if token is None:
        return None
    try:
        received = parsedate_to_datetime(headers["Date"]).astimezone(timezone.utc)
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc)
    return {
        "token": token,
        "sender": parseaddr(form.get("from", ""))[1].lower(),
        "message_id": (headers["Message-ID"] or make_msgid()).strip(),
        "references": (headers["References"] or "").strip(),
        "received_at": received.isoformat(),
        "body": strip_quoted(form.get("text") or _html_to_text(form.get("html", "")))[:MAX_BODY_CHARS],
    }


def receive(form: dict[str, str]) -> tuple[str | None, str]:
    """Validate a reply and queue it for its event. Returns (event_id, status)."""
    reply = parse_sendgrid(form)
    if reply is None:
        return None, "ignored"
    event_id = db.event_id_for_token(reply["token"])
    event = db.load_event(event_id) if event_id else None
    guest = event.guest_by_token(reply["token"]) if event else None
    if guest is None:
        return None, "unknown address"
    if reply["sender"] != guest.email.lower():
        return event_id, "sender mismatch"  # only the invited address may answer for that guest
    if not db.add_inbox(event_id, "reply", reply, dedupe_key=reply["message_id"]):
        return event_id, "duplicate"

    import worker  # local import: worker imports this module

    worker.wake(event_id)
    return event_id, "queued"


def to_sendgrid_form(sender: str, sender_name: str, to: str, subject: str, text: str,
                     in_reply_to: str | None = None, references: str = "") -> dict[str, str]:
    """The fields SendGrid Inbound Parse would post for this email (used by demo guests)."""
    message_id = make_msgid(domain=sender.rpartition("@")[2] or "demo.local")
    from_header = f"{sender_name} <{sender}>" if sender_name else sender
    lines = [
        f"From: {from_header}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {format_datetime(datetime.now(timezone.utc))}",
        f"Message-ID: {message_id}",
    ]
    if in_reply_to:
        lines += [f"In-Reply-To: {in_reply_to}", f"References: {(references + ' ' + in_reply_to).strip()}"]
    return {
        "headers": "\n".join(lines) + "\n",
        "from": from_header,
        "to": to,
        "subject": subject,
        "text": text,
        "envelope": json.dumps({"to": [to], "from": sender}),
        "charsets": json.dumps({"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "text": "UTF-8"}),
        "SPF": "pass",
    }


def _html_to_text(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", re.sub(r"<(br|/p|/div)[^>]*>", "\n", html, flags=re.I)))


# "On Sat, Nov 14, 2026 at 3:00 PM Name <x@y.com> wrote:" (sometimes wrapped onto 2 lines).
# Requires a digit so a reply starting "On Tuesday I can..." isn't cut.
_QUOTE_HEADER = re.compile(r"^On [^\n]*\d[^\n]*(\n[^\n]*)?wrote:\s*$", re.M)


def strip_quoted(text: str) -> str:
    m = _QUOTE_HEADER.search(text)
    if m:
        text = text[: m.start()]
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">")).strip()
