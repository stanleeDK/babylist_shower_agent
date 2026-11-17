"""The event chat: messages, widgets, and the setup steps.

Setup is driven by code, so clicking an option is instant: each step is an agent message
with a widget (choices, a date picker, the guest picker...). Typed messages go to Claude
(agent.setup_reply during setup, the event agent afterwards), which can fill in several
answers at once ("it's for my sister Maria, sometime in spring"). Either way, the next
unanswered step is shown next.

Messages live in the `chat` table. Widgets are dicts with a "type"; the page renders them.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parseaddr

import db
import venues
from models import POPULAR_WINDOW, SETUP_STEPS, TIMES_OF_DAY, WINDOWS, Event, window_dates_for

TIME_LABELS = {"morning": "Morning (around 11am)", "afternoon": "Afternoon (around 1pm)", "evening": "Evening (around 6pm)"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SETTING_LABELS = {"home": "At home", "out": "Out: a restaurant or park"}


def say(event_id: str, text: str, widget: dict | None = None) -> int:
    return db.add_chat(event_id, "agent", text, widget=widget)


def user_said(event_id: str, text: str, typed: bool = False) -> int:
    return db.add_chat(event_id, "user", text, kind="typed" if typed else "text")


def notice(event_id: str, text: str) -> int:
    return db.add_chat(event_id, "agent", text, kind="notice")


def email_card(event_id: str, text: str, merge_key: str = "", names: list[str] | None = None,
               items: list[dict] | None = None, label: str = "", direction: str = "out") -> None:
    """Emails in the chat, shown as a small stack of envelopes, e.g. "Invitations sent" with
    one card per person ({"token", "name", "preview"}); clicking a name opens that person.
    `text` is the one-line version ("Invitations sent to Ana, Ben and Chloe"). Consecutive
    cards with the same merge_key are combined into one stack."""
    chat = db.list_chat(event_id)
    last = chat[-1] if chat else None
    lw = (last or {}).get("widget") or {}
    if merge_key and names and last and last["kind"] == "email" and lw.get("merge") == merge_key:
        merged = list(dict.fromkeys(lw["names"] + names))
        known = {i["token"] for i in lw.get("items", [])}
        all_items = lw.get("items", []) + [i for i in items or [] if i["token"] not in known]
        db.update_chat_text(last["id"], text.format(names=join_names(merged)),
                            widget={**lw, "names": merged, "items": all_items})
        return
    db.add_chat(event_id, "agent", text.format(names=join_names(names or [])), kind="email",
                widget={"merge": merge_key, "names": names or [], "items": items or [], "label": label,
                        "dir": direction})


def preview(body: str, length: int = 110) -> str:
    """The first line worth reading: skips greetings like "Hi Ana,"."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for ln in lines:
        if not re.match(r"^(hi|hey|hello|dear|good (morning|afternoon|evening))\b", ln, re.I) or len(ln) > 40:
            return ln if len(ln) <= length else ln[:length].rsplit(" ", 1)[0] + "…"
    return lines[0][:length] if lines else ""


def person_status(event: Event, g) -> tuple[str, str]:
    """(color, label) for an invitee: green = confirmed for the date, red = can't make it,
    yellow = waiting (no reply yet, or no date settled yet)."""
    day = event.booked_date
    if g.status == "declined":
        return "red", "Can't make it" if not g.is_restaurant else "Can't host"
    if g.status == "invited":
        if g.transcript and g.transcript[-1]["direction"] == "in":
            return "yellow", "Replied, agent is reading"
        return "yellow", "Waiting for a reply" if g.transcript else "Not emailed yet"
    if day:
        if day in g.available_dates:
            return "green", "Confirmed" if not g.is_restaurant else "Can host you"
        return "red", "Can't make that date"
    proposed = (event.proposal or {}).get("date")
    if proposed and proposed in g.available_dates:
        return "yellow", "Free on the proposed date"
    return "yellow", f"Replied: free on {len(g.available_dates)} day{'s' if len(g.available_dates) != 1 else ''}"


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def nice(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x:%a %b} {x.day}"


def nice_time(hhmm: str | None) -> str:
    """'13:00' -> '1pm', '11:30' -> '11:30am'."""
    if not hhmm:
        return ""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''}{'am' if h < 12 else 'pm'}"


# ---------------- setup steps ----------------


def next_step(event: Event, connected: bool = False) -> str | None:
    # The Connect Google step is never skipped, even if this browser is already signed in:
    # every new shower goes through Google's "Allow" screen.
    for step in SETUP_STEPS:
        if step not in event.setup_done:
            return step
    return None


def present(event: Event, step: str, user: dict | None) -> None:
    """Show a setup step: an agent message plus its widget."""
    eid = event.id
    if step == "host":
        if not db.list_chat(eid):
            # One bubble per line. The venue cards are filled in from the visitor's location
            # when the page shows them.
            say(eid, "Hi, I'm your baby shower agent.")
            say(eid, "I can help you organize your baby shower.")
            say(eid, "Check out these venues near you that take large party reservations:", {"type": "inspiration"})
            say(eid, "I can email your friends and family to find a date that works for everyone.")
            say(eid, "I can put together a shopping list of food for you to order for the big day.")
            say(eid, "I stop short of booking or buying anything: you stay in control.")
        say(eid, "Who's hosting?", {"type": "host"})
    elif step == "setting":
        say(eid, "Do you want to have it at home, or go out to a restaurant or a park?",
            {"type": "choices", "step": "setting",
             "options": [{"value": k, "label": v} for k, v in SETTING_LABELS.items()]})
    elif step == "date":
        today = date.today()
        windows = []
        for n, (first, last) in WINDOWS.items():
            s, e = window_dates_for(n, today)
            windows.append({"value": n, "label": f"{nice(s)} – {nice(e)}", "sub": f"{first}–{last} days out",
                            "popular": n == POPULAR_WINDOW})
        say(eid, "Do you have a specific date in mind, or a window? If you give me a window, I'll email your "
                 "guests and find the day that works for everyone.",
            {"type": "date", "windows": windows, "note": "Most people book their shower 111–117 days out.",
             "min": (today.toordinal() + 1), "max_days": 365})
    elif step == "time":
        say(eid, "What time of day? Showers are planned for about two hours.",
            {"type": "choices", "step": "time",
             "options": [{"value": k, "label": TIME_LABELS[k]} for k in TIMES_OF_DAY]})
    elif step == "gmail":
        say(eid, "Give me permission to connect to your Gmail and I can email your invitees and organize their "
                 "RSVPs on your behalf. I can also email restaurants about availability. I only send email "
                 "(I never read your inbox) and add the event to your Google Calendar.",
            {"type": "gmail"})
    elif step == "guests":
        say(eid, "Who's invited? Start typing a name to search your contacts, or add the sample guests to try "
                 "it with simulated guests. Tap the star next to anyone whose attendance is a must.", {"type": "guests"})


def advance(event: Event, user: dict | None) -> str | None:
    """Show the next unanswered step, if any. Returns it."""
    step = next_step(event)
    if step:
        present(event, step, user)
    return step


def apply_setup_answer(event: Event, step: str, value) -> str | None:
    """Store a setup answer. Returns the text of the user's bubble, or None if invalid."""
    if step == "host":
        if not isinstance(value, dict):
            return None
        if value.get("me"):
            event.host_name = event.host_email = ""
            text = "I'm hosting"
        else:
            name = str(value.get("name") or "").strip()[:60]
            email = parseaddr(str(value.get("email") or "").strip())[1].lower()
            if not name or not EMAIL.match(email):
                return None
            event.host_name, event.host_email = name, email
            text = f"{name} is hosting ({email})"
    elif step == "setting":
        if value not in SETTING_LABELS:
            return None
        event.setting = value
        text = SETTING_LABELS[value]
    elif step == "date":
        if not isinstance(value, dict):
            return None
        if value.get("mode") == "specific":
            try:
                d = date.fromisoformat(str(value.get("date")))
            except ValueError:
                return None
            if d <= date.today() or (d - date.today()).days > 365:
                return None
            event.date_mode, event.window_bucket = "specific", 0
            event.window_start = event.window_end = d.isoformat()
            text = f"On {nice(d.isoformat())}"
        elif value.get("mode") == "window" and value.get("bucket") in WINDOWS:
            n = value["bucket"]
            event.date_mode, event.window_bucket = "window", n
            event.window_start, event.window_end = window_dates_for(n)
            text = f"Sometime {nice(event.window_start)} – {nice(event.window_end)}"
        else:
            return None
    elif step == "time":
        if value not in TIMES_OF_DAY:
            return None
        event.time_of_day, event.start_time = value, TIMES_OF_DAY[value]
        text = TIME_LABELS[value]
    else:
        return None
    if step not in event.setup_done:
        event.setup_done.append(step)
    return text


def maps_url(v: dict) -> str:
    """Google Maps search for the place, which opens its Maps card."""
    from urllib.parse import quote_plus

    where = v.get("address") or (f"{v['park']}, San Francisco, CA" if v.get("park") else "San Francisco, CA")
    name = v["name"] if v["id"].startswith("resy") else v.get("park", v["name"])
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name}, {where}')}"


def inspiration(event: Event) -> list[dict]:
    """The intro's venue cards: the two closest restaurants and the closest park."""
    near = venues.nearest(event.location, n=2)
    picks = near["restaurants"][:2] + near["picnic"][:1]
    out = []
    for v in picks:
        restaurant = v["id"].startswith("resy")
        out.append({
            "name": v["name"] if restaurant else v["park"],
            "kind": "restaurant" if restaurant else "picnic",
            "photo": v.get("photo", ""),
            "line1": (" · ".join(x for x in [", ".join(v.get("cuisine") or []), v.get("neighborhood")] if x)
                      if restaurant else v["name"]),
            "line2": (f"Takes groups of 10 · {v['miles']} mi" if restaurant
                      else f"Picnic area for up to {v.get('capacity') or 50} · {v['miles']} mi"),
            "url": maps_url(v),
        })
    return out


def venue_options(event: Event) -> list[dict]:
    """The venues to offer: picnic alternatives if the chosen one was taken, else the nearest."""
    if event.picnic_alternatives:
        return [v for v in (venues.by_id(i) for i in event.picnic_alternatives) if v]
    near = venues.nearest(event.location)
    return near["restaurants"] + near["picnic"]


def offer_venues(event: Event, text: str) -> None:
    opts = venue_options(event)
    say(event.id, text, {"type": "venue", "options": [
        {"id": v["id"], "name": v["name"], "kind": "picnic" if v["id"].startswith("picnic") else "restaurant",
         "sub": v.get("park") or v.get("neighborhood") or "", "photo": v.get("photo", "")} for v in opts]})


def offer_shopping(event: Event, where: str) -> None:
    say(event.id, f"Want me to put together a shopping list for {where}? About {event.headcount} people, 10 "
                  f"items, found at your nearest Kroger-family store, with photos and prices.",
        {"type": "shopping_offer"})


def ask_new_date(event: Event, text: str) -> None:
    today = date.today()
    windows = []
    for n, (first, last) in WINDOWS.items():
        s, e = window_dates_for(n, today)
        windows.append({"value": n, "label": f"{nice(s)} – {nice(e)}", "sub": f"{first}–{last} days out",
                        "popular": n == POPULAR_WINDOW})
    say(event.id, text, {"type": "new_date", "windows": windows,
                         "note": "A specific date means guests get an RSVP for that day; a window means I find "
                                 "the day that works for everyone again."})
