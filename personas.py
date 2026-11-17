"""Simulated people: demo guests, and the restaurant (always simulated for now).

They answer the agent's emails in character within seconds. Their replies are built in
SendGrid Inbound Parse format and go through inbound.receive, the exact path a real reply
takes in production.

Restaurants: the agent's emails go to a plus-address of the organizer's own Gmail (never
to the real restaurant), and a simulated events manager answers. On a date window it's
open every day except two Saturdays the guests can't do anyway, so it shows up in the
availability grid without ever blocking the answer.
"""
from __future__ import annotations

import random
import threading
import time
from datetime import date, datetime

import config
from agent import client
from inbound import receive, reply_address, to_sendgrid_form
from models import Event, Guest

MAX_REPLIES_PER_GUEST = 10

# In demo mode every guest gets one of these, in order, whoever they are (sample guests
# or people picked from your contacts). They're written about days, relative to the date
# window, so they fit any window of two weeks or more. Together they leave one date
# that works for everyone: the second Sunday. Ben makes the agent ask a follow-up, and
# Chloe's soft "rather not Sundays" gives it something to weigh.
PERSONA_POOL = [
    "You're away visiting family the entire first weekend of the window. Every other day "
    "works. You reply promptly and spell out exactly which days work.",
    "You work weekdays, so only Saturdays and Sundays work for you, and you're at a friend's "
    "wedding on the second Saturday of the window. At first you answer vaguely ('weekends are "
    "usually best for me') and only give specific dates once someone asks you directly.",  # the agent follows up
    "You're on a work trip from the first Tuesday through the first Friday of the window, and "
    "otherwise free. You'd rather avoid Sundays because of early Monday starts. Say so, but "
    "add that you could make a Sunday work if it's the only option.",
    "You're busy the whole first weekend of the window and the second Saturday (your sister's "
    "birthday), and free on every other day. You write warm, chatty emails full of excitement "
    "about the baby.",
    "Any weekend day works for you. Before giving dates, you first ask whether it's during the "
    "day, then answer once you know.",
    "Your weekends are packed except the second weekend of the window, and you can only do "
    "the Sunday of it. You keep replies to one or two lines.",
]

# A host who isn't the organizer: flexible, so the shower can happen.
HOST_PERSONA = ("You're hosting this baby shower, so you'll make any weekend day work. You reply promptly and "
                "list the weekend days in the window.")

SAMPLE_GUESTS = [{"name": n} for n in ("Ana Ruiz", "Ben Okafor", "Chloe Martin")]


def persona_for(index: int) -> str:
    return PERSONA_POOL[index % len(PERSONA_POOL)]


SYSTEM = """\
You are role-playing {name}, a guest in an email conversation, for a product demo. \
Someone is organizing a baby shower and emailing you about it.

Who you are and your real schedule:
{persona}

Today is {today}. The dates being discussed, with weekdays:
{calendar}

Write {name}'s reply to the latest email. Keep it short and casual, the way people \
answer a friend: plain text, no emojis, no subject line, no quoted text, signed with \
your first name. Stay true to your schedule and to what you said before, and answer the way your \
persona says you answer, even if that's vague. Never say you'll check and get back to \
them later: you only write when you get an email, so answer now with what you know. \
You're coming to the shower: never say \
you can't come at all, and never invent conflicts beyond your schedule above (dropping \
out only happens when the demo triggers it). If you earlier said you couldn't make a \
date because something came up, that was only that one date: any new date works for \
you, as far as your schedule above allows. If the latest \
email is a final confirmation or doesn't need an answer, reply with exactly \
NO_REPLY.{situation}"""

RSVP = """

This invitation is for one fixed date ({date}), and you can make it: say yes warmly. \
That holds even if you couldn't make an earlier date: whatever came up then doesn't \
affect this one. Answer later emails (like a final confirmation) with NO_REPLY unless a \
question needs an answer."""

# Once a date is booked, guests always take it. Dropping out only happens when the demo
# button triggers it, never because the booked date clashes with a persona's schedule.
BOOKED = """

The shower is now booked for {date}, and you're coming. This overrides your schedule \
above: you've moved things around, so that date works for you. Never say you can't make \
it, and don't ask to change the date. If the email confirms the booking or asks whether \
it works, reply with a brief, happy yes. If it doesn't need an answer, reply with exactly \
NO_REPLY."""

CANT_MAKE_IT = """

What's happening now: the event is booked for {date}, but something has come up and \
you can no longer make that one day. Write a new message in this thread telling the \
organizer (this message is needed, so don't answer NO_REPLY). Keep it in character, \
apologetic, and brief, with a believable reason. Say other days should be fine; don't \
name any other dates you can't do."""


def plus_address(organizer_email: str, name: str, taken: set[str] = frozenset()) -> str:
    """Ana -> you+ana@gmail.com, so demo invitations land in the organizer's own inbox."""
    local, _, domain = organizer_email.partition("@")
    base = "".join(ch for ch in (name.split() or ["guest"])[0].lower() if ch.isalnum()) or "guest"
    tag, n = base, 2
    while f"{local.split('+')[0]}+{tag}@{domain}" in taken:
        tag, n = f"{base}{n}", n + 1
    return f"{local.split('+')[0]}+{tag}@{domain}"


RESTAURANT_SYSTEM = """\
You are role-playing the events manager at {name}, a San Francisco restaurant, answering \
an email about a group booking, for a product demo.

Today is {today}. The party: a baby shower for about {party} people at {time}.
{availability}

Write the reply to the latest email: short, friendly and professional, plain text, no \
emojis, no subject line, signed "{signoff}". Answer what they asked. If they ask which dates work, \
state your availability as given (you can describe it as "every day from ... to ... \
except ..."). If they confirm a headcount or a date you can do, confirm it and say the \
table is held pending their booking. Don't take payment or ask for a deposit. If the \
latest email doesn't need an answer, reply with exactly NO_REPLY."""


def restaurant_availability(event: Event) -> tuple[list[str], list[str]]:
    """(open dates, fully booked dates) in the event's window."""
    dates = event.window_dates()
    if len(dates) == 1:
        return dates, []
    saturdays = [d for d in dates if date.fromisoformat(d).weekday() == 5]
    # The 2nd Saturday (Ben's wedding) and the 4th: dates the demo guests can't do or
    # won't pick, so the restaurant row shows up in the grid without blocking the answer.
    booked = [saturdays[i] for i in (1, 3) if i < len(saturdays)]
    return [d for d in dates if d not in booked], booked


def on_sent(event: Event, guest: Guest) -> None:
    """Called by the agent after it emails someone. Answers as them a few seconds later."""
    if not guest.persona or sum(t["direction"] == "in" for t in guest.transcript) >= MAX_REPLIES_PER_GUEST:
        return
    threading.Thread(target=_reply_later, args=(_snapshot(event, guest),), daemon=True).start()


def simulate_cant_make_it(event: Event, guest: Guest) -> None:
    """Demo button: this guest emails out of the blue to say they can't make the booked date."""
    s = _snapshot(event, guest)
    s["situation"] = CANT_MAKE_IT.format(date=f"{event.booked_date} ({date.fromisoformat(event.booked_date):%A})")
    s["delay"] = 1
    threading.Thread(target=_reply_later, args=(s,), daemon=True).start()


def _snapshot(event: Event, guest: Guest) -> dict:
    # Taken now: the event keeps changing while the guest "thinks".
    from agent import email_subject

    s = {
        "name": guest.name or guest.email,
        "email": guest.email,
        "token": guest.token,
        "persona": guest.persona,
        "kind": guest.kind,
        "transcript": [dict(t) for t in guest.transcript],
        "organizer_first": event.organizer_name.split()[0] if event.organizer_name else "Organizer",
        "calendar": event.window_calendar(),
        "subject": "Re: " + email_subject(event, guest),
        "in_reply_to": guest.last_message_id,
        "references": guest.last_references,
        "situation": "",
        "delay": config.DEMO_REPLY_DELAY_SECONDS + random.uniform(0, config.DEMO_REPLY_JITTER_SECONDS),
    }
    if event.date_mode == "specific" and guest.kind == "guest":
        s["situation"] = RSVP.format(date=f"{event.window_start} ({date.fromisoformat(event.window_start):%A})")
    elif event.status == "booked" and event.booked_date and guest.kind == "guest":
        s["situation"] = BOOKED.format(date=f"{event.booked_date} ({date.fromisoformat(event.booked_date):%A})")
    if guest.kind == "restaurant":
        open_dates, booked = restaurant_availability(event)
        if len(event.window_dates()) == 1:
            availability = f"You can host them on {event.window_start} ({date.fromisoformat(event.window_start):%A}) at that time."
        else:
            availability = (f"Your availability for groups: open every day from {event.window_start} to "
                            f"{event.window_end} except {', '.join(f'{d} ({date.fromisoformat(d):%A})' for d in booked) or 'none'}, "
                            "which are fully booked.")
        s.update(party=event.headcount, time=f"{event.start_time} ({event.time_of_day})", availability=availability,
                 signoff=f"Events team, {guest.name}")
    return s


def _reply_later(s: dict) -> None:
    time.sleep(s["delay"])
    try:
        text = write_reply(s)
        if text:
            receive(to_sendgrid_form(s["email"], s["name"], reply_address(s["token"]), s["subject"], text,
                                     in_reply_to=s["in_reply_to"], references=s["references"]))
    except Exception as e:  # a demo hiccup should never take down the app
        print(f"[demo] reply from {s['email']} failed: {e!r}")


def write_reply(s: dict) -> str | None:
    convo = "\n\n".join(
        f"--- {s['organizer_first'] if t['direction'] == 'out' else 'You'} wrote:\n{t['body']}" for t in s["transcript"]
    )
    today = f"{datetime.now():%Y-%m-%d %a}"
    if s.get("kind") == "restaurant":
        system = RESTAURANT_SYSTEM.format(name=s["name"], today=today, party=s["party"], time=s["time"],
                                          availability=s["availability"], signoff=s["signoff"])
    else:
        system = SYSTEM.format(name=s["name"], persona=s["persona"], today=today, calendar=s["calendar"],
                               situation=s["situation"])
    response = client.messages.create(
        model=config.FAST_MODEL,  # quick, natural replies keep the demo moving
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": f"The conversation so far:\n\n{convo}\n\nWrite your reply."}],
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
    )
    if response.stop_reason == "refusal":
        return None
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return None if not text or text == "NO_REPLY" else text
