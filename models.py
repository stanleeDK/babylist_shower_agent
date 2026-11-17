"""Event and guest state, plus the date arithmetic the agent's tools rely on."""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone

MAX_WINDOW_DAYS = 150

# Date windows offered during setup: (first day, last day), counted from today.
# Most people book their shower 111-117 days out, which is in window 3.
WINDOWS = {1: (1, 88), 2: (89, 111), 3: (112, 149)}
POPULAR_WINDOW = 3

# Time of day -> start time. Restaurants are asked for this time; picnic searches use
# half-day blocks (evening falls back to the afternoon, parks close at dusk).
TIMES_OF_DAY = {"morning": "11:00", "afternoon": "13:00", "evening": "18:00"}
DURATION_MINUTES = 120

SETUP_STEPS = ["host", "setting", "date", "time", "gmail", "guests"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def window_dates_for(bucket: int, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    first, last = WINDOWS[bucket]
    return (today + timedelta(days=first)).isoformat(), (today + timedelta(days=last)).isoformat()


@dataclass
class Guest:
    """Someone the agent emails: a guest, or the restaurant (kind="restaurant")."""

    email: str
    name: str = ""
    token: str = field(default_factory=lambda: secrets.token_hex(6))  # routes their replies
    kind: str = "guest"  # guest | restaurant
    key: bool = False  # a key person: if they can't come, the agent suggests rescheduling
    role: str = ""  # "host" for a host who isn't the organizer
    real_email: str = ""  # restaurant's real address; emails actually go to `email` (a plus-address)
    persona: str = ""  # demo mode: how the simulated guest behaves ("restaurant" for venues)
    status: str = "invited"  # invited -> responded | declined
    available_dates: list[str] = field(default_factory=list)
    notes: str = ""
    transcript: list[dict] = field(default_factory=list)  # {"direction": "out"|"in", "at", "body"}
    last_contacted: str | None = None
    last_reply_at: str | None = None
    reminders_sent: int = 0
    # Rounds: round 0 is the first search for a date; each reschedule starts a new round.
    round_contacted: int = -1  # last round we've emailed them in (a new round's first email isn't a "chase")
    confirmed_round: int = -1  # last round they got a booking confirmation for
    gmail_thread_id: str | None = None
    last_message_id: str | None = None  # their latest Message-ID, for In-Reply-To
    last_references: str = ""
    stall_nudged_for: str = ""  # demo: the reply we already prodded the agent about

    @property
    def display(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email

    @property
    def first_name(self) -> str:
        if self.kind == "restaurant":
            return self.name or self.email
        return (self.name or self.email).split()[0]

    @property
    def is_restaurant(self) -> bool:
        return self.kind == "restaurant"


@dataclass
class Event:
    id: str
    user_id: str  # "" while an anonymous visitor is still setting up
    organizer_name: str = ""
    organizer_email: str = ""
    title: str = "Baby shower"
    window_start: str = ""  # YYYY-MM-DD inclusive; for a specific date, start == end
    window_end: str = ""
    timezone: str = "America/Los_Angeles"  # organizer's IANA timezone, from their browser
    guests: list[Guest] = field(default_factory=list)
    description: str = ""
    area: str = ""
    start_time: str | None = None  # "HH:MM"
    time_of_day: str = ""  # morning | afternoon | evening
    # Who's hosting: "" = the organizer. Another host is invited as a key person.
    host_name: str = ""
    host_email: str = ""
    registry_url: str = ""  # the baby registry, linked in every invitation (a placeholder for the demo)
    duration_minutes: int = DURATION_MINUTES
    demo: bool = False

    # Setup (the chat before the agent starts)
    setup_done: list[str] = field(default_factory=list)  # answered SETUP_STEPS
    setting: str = ""  # home | out
    date_mode: str = ""  # specific | window
    window_bucket: int = 0  # which WINDOWS entry, for window mode
    # {"city", "region", "postal", "lat", "lng", "in_sf", "label"} from the visitor's IP address
    location: dict = field(default_factory=dict)

    # The venue, once chosen. Restaurants are also in `guests` (kind="restaurant").
    # {"type": "restaurant"|"picnic", "id", "name", "photo", "url", "address",
    #  "token" (restaurant), "booked" (picnic), "check" (latest live availability check)}
    venue: dict | None = None
    picnic_alternatives: list[str] = field(default_factory=list)  # free tables to switch to
    pending_book: str | None = None  # a date the organizer chose, waiting on them to book the venue
    venue_nudges: int = 0  # reminders sent so far to pick a venue (going out, none chosen yet)
    last_venue_nudge: str | None = None

    # setup -> collecting -> proposed -> booked -> done (date passed)
    #                                       \-> collecting again when rescheduling
    # A specific date goes straight from setup to booked.
    # stopped: the organizer turned the agent off
    status: str = "setup"
    round: int = 0
    proposal: dict | None = None  # {"date", "reasoning", "alternatives"}
    booked_date: str | None = None
    calendar_event_id: str | None = None  # so a reschedule moves the event instead of duplicating it
    calendar_link: str | None = None
    calendar_date: str | None = None  # date currently on Google Calendar (kept while rescheduling)
    previous_dates: list[str] = field(default_factory=list)
    # A question the agent is waiting on the organizer to answer with a button:
    # {"id", "question", "approve_label", "decline_label", "purpose": "reschedule" | "other"}
    pending_question: dict | None = None
    activity: list[dict] = field(default_factory=list)  # {"at", "kind", "text"}
    consecutive_failures: int = 0
    unhandled: list[str] = field(default_factory=list)  # what a failed run didn't finish
    created_at: str = field(default_factory=lambda: now_utc().isoformat())

    # ---------- serialization ----------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        # Ignore fields from older versions so saved events keep loading after upgrades.
        known_event, known_guest = {f.name for f in fields(cls)}, {f.name for f in fields(Guest)}
        d = {k: v for k, v in d.items() if k in known_event}
        d["guests"] = [Guest(**{k: v for k, v in g.items() if k in known_guest}) for g in d.get("guests", [])]
        return cls(**d)

    # ---------- helpers ----------

    def log(self, kind: str, text: str) -> None:
        """kind: thinking | action | email_out | email_in | note | organizer | system | error | question | lookup"""
        self.activity.append({"at": now_utc().isoformat(), "kind": kind, "text": text})

    def guest(self, email: str) -> Guest | None:
        email = email.strip().lower()
        return next((g for g in self.guests if g.email.lower() == email), None)

    def guest_by_token(self, token: str) -> Guest | None:
        return next((g for g in self.guests if g.token == token), None)

    def days_until(self, day: str | None, today: date | None = None) -> int | None:
        if not day:
            return None
        return (date.fromisoformat(day) - (today or date.today())).days

    @property
    def people(self) -> list[Guest]:
        """The invited guests (not the restaurant)."""
        return [g for g in self.guests if g.kind == "guest"]

    @property
    def restaurant(self) -> Guest | None:
        """The current restaurant, if the venue is one."""
        if self.venue and self.venue.get("type") == "restaurant":
            return self.guest_by_token(self.venue.get("token", ""))
        return None

    @property
    def attending(self) -> list[Guest]:
        """Everyone whose availability decides the date: guests who haven't declined, plus the
        current restaurant."""
        out = [g for g in self.people if g.status != "declined"]
        r = self.restaurant
        if r and r.status != "declined":
            out.append(r)
        return out

    @property
    def headcount(self) -> int:
        """Party size to plan for: guests who haven't declined, plus the organizer."""
        return len([g for g in self.people if g.status != "declined"]) + 1

    @property
    def is_setup(self) -> bool:
        return self.status == "setup"

    def window_dates(self) -> list[str]:
        if not (self.window_start and self.window_end):
            return []
        start, end = date.fromisoformat(self.window_start), date.fromisoformat(self.window_end)
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    def window_calendar(self) -> str:
        return "\n".join(f"{d} {date.fromisoformat(d):%a}" for d in self.window_dates())

    def grid_dates(self) -> list[str]:
        """Columns for the availability grid: every day of a short window; for a long one,
        weekends plus any day someone can do (88 columns wouldn't fit)."""
        dates = self.window_dates()
        if len(dates) <= 21:
            return dates
        someone = {d for g in self.attending for d in g.available_dates}
        keep = [d for d in dates if date.fromisoformat(d).weekday() >= 5 or d in someone]
        return keep or dates[:21]

    def date_summary(self, exclude_dates: list[str] | None = None) -> dict:
        """Who's still pending, which dates work for everyone attending (including the
        restaurant), and the best partial dates."""
        exclude = set(exclude_dates or [])
        window = [d for d in self.window_dates() if d not in exclude]
        attending = self.attending
        responded = [g for g in attending if g.status == "responded"]
        pending = [g.email for g in attending if g.status == "invited"]

        free_by_date = {d: [g.email for g in responded if d in g.available_dates] for d in window}
        everyone = [d for d, free in free_by_date.items() if attending and len(free) == len(attending)]
        best = sorted((item for item in free_by_date.items() if item[1]), key=lambda i: (-len(i[1]), i[0]))[:5]
        return {
            "attending": len(attending),
            "pending_responses": pending,
            "declined": [g.email for g in self.guests if g.status == "declined"],
            "all_responded": not pending,
            "dates_everyone_free": everyone,
            "best_partial_dates": [
                {
                    "date": d,
                    "free_count": len(free),
                    "missing": [g.email for g in responded if g.email not in free] + pending,
                }
                for d, free in best
            ],
        }
