"""Background worker: wakes the agent whenever something happens to an event.

There is no loop that runs forever. An event rests in the database between wakes; each
wake is one bounded agent run. Wakes come from: the organizer finishing setup, a guest's
or restaurant's email, the organizer (a message, a button), or the ticker (an invitation
or reminder is due). An event ends when the organizer stops it or its date passes.

A single worker thread processes events one at a time, so an event's state is only ever
written from here once setup is over. Web requests and inbound email just add to the
event's inbox and call wake(). Things that aren't the agent's judgment (adding the event
to the calendar, live venue checks, moving the date the organizer picked) are done here
in code, and the agent is told about them.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import chat
import db
import venues
from agent import MAX_REMINDERS, REMINDER_MIN_HOURS, run_agent
from google_api import GoogleAccount
from models import Event, Guest, now_utc, parse_iso, window_dates_for

TICK_SECONDS = 15
# Going out but no venue picked: after this much quiet, remind the organizer (at most
# VENUE_NUDGES_MAX times). Demo events are fast, so the pause is short.
VENUE_QUIET_SECONDS = {True: 40, False: 6 * 3600}
VENUE_NUDGES_MAX = 3
# Groceries: order this many days before the shower (Kroger's API can't schedule
# deliveries, so we wake up and order). If the organizer says "not yet", ask again later.
ORDER_DAYS_BEFORE = 2
PAYMENT_RETRY = {True: timedelta(minutes=2), False: timedelta(days=1)}
MAX_CONSECUTIVE_FAILURES = 3

_queue: "queue.Queue[str]" = queue.Queue()
_queued: set[str] = set()
_guard = threading.Lock()
_started = False
busy: set[str] = set()  # events the agent is working on right now (the page shows "thinking")


def wake(event_id: str) -> None:
    with _guard:
        if event_id in _queued:
            return
        _queued.add(event_id)
    _queue.put(event_id)


def is_busy(event_id: str) -> bool:
    return event_id in busy or event_id in _queued


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_work_forever, daemon=True, name="agent-worker").start()
    threading.Thread(target=_tick_forever, daemon=True, name="agent-ticker").start()


def _work_forever() -> None:
    while True:
        event_id = _queue.get()
        with _guard:
            _queued.discard(event_id)  # a wake() during processing queues another pass
        busy.add(event_id)
        try:
            process(event_id)
        except Exception:
            traceback.print_exc()
        finally:
            busy.discard(event_id)


def _tick_forever() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        try:
            for event in db.list_events():
                if event.status == "booked" and _date_passed(event):
                    wake(event.id)  # process() marks it done
                elif event.status in ("collecting", "proposed", "booked") and \
                        event.consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                    if due_items(event) or event.unhandled or db.pending_inbox(event.id) or delivery_due(event):
                        wake(event.id)
        except Exception:
            traceback.print_exc()


def _date_passed(event: Event) -> bool:
    today = now_utc().astimezone(ZoneInfo(event.timezone)).date()
    return bool(event.booked_date) and date.fromisoformat(event.booked_date) < today


def due_items(event: Event) -> list[str]:
    """Invitations, date-change notices and reminders that are due now."""
    if event.status not in ("collecting", "proposed", "booked"):
        return []
    items = []
    for g in event.attending:
        what = "an availability request (the restaurant)" if g.is_restaurant else "an invitation"
        if g.round_contacted < event.round or not g.last_contacted:
            items.append(f"Needs {what if event.round == 0 or not g.transcript else 'to hear the date is moving'}: {g.display}")
        elif stalled(event, g):
            items.append(f"Unanswered reply: {g.display} replied but hasn't given usable dates, and nobody has "
                         "written back. Send one short, specific follow-up now (e.g. which weekends work).")
        elif g.status == "invited" and g.reminders_sent < MAX_REMINDERS and event.status != "proposed":
            hours = (now_utc() - parse_iso(g.last_contacted)).total_seconds() / 3600
            if hours >= REMINDER_MIN_HOURS:
                items.append(f"Reminder due: {g.display} hasn't replied in {hours / 24:.0f} days.")
    if venue_nudge_due(event):
        items.append("Venue reminder due: the organizer is going out but hasn't picked a venue, and things have "
                     "gone quiet. Call offer_venues with one or two friendly sentences: the venue is best lined up "
                     "alongside the date, so you can check it's free on the day everyone can make.")
    return items


STALL_SECONDS = 25


def stalled(event: Event, g: Guest) -> bool:
    """Demo safety net: a guest replied vaguely and the agent decided to wait. Simulated
    guests only write when emailed, so waiting would stall the demo forever."""
    if not event.demo or g.status != "invited" or not g.transcript or g.transcript[-1]["direction"] != "in":
        return False
    if g.stall_nudged_for == g.last_reply_at:
        return False  # already prodded about this reply
    return (now_utc() - parse_iso(g.last_reply_at)).total_seconds() >= STALL_SECONDS


def needs_venue(event: Event) -> bool:
    return event.setting == "out" and not event.venue and event.status in ("collecting", "proposed", "booked")


def venue_nudge_due(event: Event) -> bool:
    if not needs_venue(event) or event.venue_nudges >= VENUE_NUDGES_MAX:
        return False
    times = [parse_iso(a["at"]) for a in event.activity[-1:]]
    if event.last_venue_nudge:
        times.append(parse_iso(event.last_venue_nudge))
    if not times:
        return False
    gap = VENUE_QUIET_SECONDS[event.demo] * (event.venue_nudges + 1)  # each reminder waits a little longer
    return (now_utc() - max(times)).total_seconds() >= gap


def process(event_id: str, google_factory=None) -> None:
    event = db.load_event(event_id)
    if event is None or event.status == "setup":
        return
    items = db.pending_inbox(event_id)
    happenings: list[str] = list(event.unhandled)  # anything a failed run didn't get to
    google = None

    def get_google():
        nonlocal google
        if google is None:
            token = db.get_user_token(event.user_id)
            if token is None:
                raise RuntimeError("The organizer has disconnected Gmail.")
            google = (google_factory or GoogleAccount)(token)
        return google

    organizer_spoke = False
    for item in items:
        kind, p = item["kind"], item["payload"]
        try:
            if kind == "activated":
                happenings.append(_activation_note(event))
            elif kind == "reply":
                happenings.append(_apply_reply(event, p))
            elif kind == "organizer_note":
                organizer_spoke = True
                event.log("organizer", p["text"])
                happenings.append(f"The organizer wrote to you in the chat:\n{p['text']}")
            elif kind == "demo_log":
                event.log("system", p["text"])
            elif kind == "answer":
                note = _apply_answer(event, p)
                if note:
                    happenings.append(note)
            elif kind == "stop":
                event.status = "stopped"
                event.log("system", "The organizer stopped the agent.")
                chat.notice(event.id, "The agent is stopped. It won't send anything else for this event.")
            elif kind == "book":
                note = _book(event, p["date"], get_google)
                if note:
                    happenings.append(note)
            elif kind == "venue":
                note = _apply_venue(event, p["venue_id"])
                if note:
                    happenings.append(note)
            elif kind in ("venue_booked", "picnic_booked"):
                note = _apply_venue_booked(event, bool(p.get("booked")), p.get("day"), get_google)
                if note:
                    happenings.append(note)
            elif kind == "new_date":
                note = _reschedule(event, p, get_google)
                if note:
                    happenings.append(note)
            elif kind == "payment":
                _apply_payment(event, bool(p.get("ok")))
        except Exception as e:  # e.g. Google rejected a calendar request; show it rather than retry forever
            traceback.print_exc()
            event.log("error", f"Couldn't handle {kind}: {e}")

    try:
        _ensure_calendar(event, get_google)
    except Exception as e:
        event.log("error", f"Couldn't add the event to Google Calendar: {e}")
    try:
        _delivery(event, get_google)
    except Exception as e:
        traceback.print_exc()
        event.log("error", f"Grocery delivery: {e}")

    if event.status == "booked" and _date_passed(event):
        event.status = "done"
        event.log("system", "The event date has passed. The agent has finished with this event.")
    if event.status in ("stopped", "done"):
        happenings = []  # replies are still recorded above, but the agent no longer acts
    else:
        due = due_items(event)
        for g in event.attending:
            if stalled(event, g):
                g.stall_nudged_for = g.last_reply_at  # prod the agent once per reply, never in a loop
        if any(d.startswith("Venue reminder due") for d in due):
            event.venue_nudges += 1
            event.last_venue_nudge = now_utc().isoformat()
        happenings += due

    if happenings:
        event.unhandled = happenings  # kept until a run succeeds
        db.save_event(event)
        db.mark_processed([i["id"] for i in items])
        try:
            run_agent(
                event,
                get_google(),
                happenings,
                save=lambda: db.save_event(event),
                on_sent=_on_sent_hook(),
                organizer_spoke=organizer_spoke,
                shopping_data=db.get_shopping(event.id),
            )
            event.unhandled, event.consecutive_failures = [], 0
        except Exception as e:
            event.consecutive_failures += 1
            event.log("error", f"The agent hit a problem and will retry: {e}")
            traceback.print_exc()
        _maybe_prompt_restaurant_booking(event)
        try:  # the organizer may have booked the venue
            _ensure_calendar(event, get_google)
        except Exception as e:
            event.log("error", f"Couldn't add the event to Google Calendar: {e}")
    db.save_event(event)
    db.mark_processed([i["id"] for i in items])


# ---------------- what happened ----------------


def _activation_note(event: Event) -> str:
    when = (f"on {event.booked_date} ({date.fromisoformat(event.booked_date):%A}) - a fixed date, so send RSVP invitations"
            if event.date_mode == "specific" else f"sometime between {event.window_start} and {event.window_end}")
    where = "at the organizer's home" if event.setting == "home" else "out (the organizer is choosing a restaurant or picnic area)"
    return (f"The organizer just finished setting up the baby shower: {when}, {event.time_of_day}, {where}. "
            f"Send the invitations now.")


def _apply_reply(event: Event, reply: dict) -> str:
    g = event.guest_by_token(reply["token"])
    g.last_message_id = reply["message_id"]
    g.last_references = reply["references"]
    if not g.last_reply_at or reply["received_at"] > g.last_reply_at:
        g.last_reply_at = reply["received_at"]
    g.transcript.append({"direction": "in", "at": reply["received_at"], "body": reply["body"]})
    event.log("email_in", f"{g.first_name} replied")
    if g.is_restaurant:
        chat.email_card(event.id, f"{g.name} replied", direction="in", label="Reply from the restaurant",
                        items=[{"token": g.token, "name": g.name, "preview": chat.preview(reply["body"])}])
    else:
        chat.email_card(event.id, "Replies from {names}", merge_key=f"in-{event.round}", names=[g.first_name],
                        items=[{"token": g.token, "name": g.name or g.first_name, "preview": chat.preview(reply["body"])}],
                        label="Replies", direction="in")
    who = f"the restaurant {g.name}" if g.is_restaurant else g.display
    return f"New reply from {who}:\n<email>\n{reply['body']}\n</email>"


def _apply_answer(event: Event, answer: dict) -> str | None:
    q = event.pending_question
    if not q or q["id"] != answer.get("question_id"):
        return None  # stale click (question already answered)
    approved = answer.get("choice") == "approve"
    label = q["approve_label"] if approved else q["decline_label"]
    event.pending_question = None
    event.log("organizer", f"Chose: {label}")
    if approved and q["purpose"] == "reschedule":
        chat.ask_new_date(event, "OK, let's find a new date. A specific day, or a window?")
        return (f'The organizer answered your question ("{q["question"]}"): they chose "{label}". They\'re picking '
                "the new date now; you'll be woken once they have. Don't email anyone yet.")
    return f'The organizer answered your question ("{q["question"]}"): they chose "{label}".'


# ---------------- calendar ----------------


def _venue_settled(event: Event) -> bool:
    if event.setting == "home":
        return True
    if not event.venue:
        return False
    return bool(event.venue.get("booked")) and event.venue.get("booked_for") in (None, event.booked_date)


def _location(event: Event) -> str:
    if event.venue:
        return ", ".join(x for x in [event.venue.get("name"), event.venue.get("address")] if x)
    return ""


def _ensure_calendar(event: Event, get_google) -> None:
    """A specific date goes on the calendar once it's settled: right away at home, once
    the restaurant says yes or the picnic area is booked."""
    if not (event.date_mode == "specific" and event.status == "booked" and event.booked_date):
        return
    if not _venue_settled(event):
        return
    attendees = [g.email for g in event.people if g.status != "declined"]
    google = get_google()
    if event.calendar_event_id:
        if event.calendar_date == event.booked_date:
            return
        event.calendar_link = google.move_event(
            event.calendar_event_id, event.booked_date, event.timezone, attendees,
            start_time=event.start_time, duration_minutes=event.duration_minutes,
        ) or event.calendar_link
        verb = "Moved the calendar event to"
    else:
        event.calendar_event_id, event.calendar_link = google.create_event(
            title=event.title, day=event.booked_date, tz_name=event.timezone, attendees=attendees,
            description=event.description, location=_location(event),
            start_time=event.start_time, duration_minutes=event.duration_minutes,
        )
        verb = "Added to your Google Calendar:"
    event.calendar_date = event.booked_date
    event.log("system", f"{verb} {chat.nice(event.booked_date)}. Google Calendar emailed the guests the invite.")
    chat.notice(event.id, f"{verb} {chat.nice(event.booked_date)} at {chat.nice_time(event.start_time)}. "
                          "Google Calendar sent your guests the invite.")


def _book(event: Event, day: str, get_google) -> str | None:
    """Window mode: the organizer clicked Book on the agent's proposal."""
    if event.status != "proposed":
        event.log("system", f"Can't book: the event is {event.status}.")
        return None
    summary = event.date_summary()
    if day not in summary["dates_everyone_free"]:
        event.log("error", f"Can't book {day}: it no longer works for everyone.")
        return f"The organizer tried to book {day}, but it no longer works for everyone. Re-check and propose again."
    if event.setting == "out" and not (event.venue and event.venue.get("booked") and event.venue.get("booked_for") == day):
        event.pending_book = day  # the venue gets booked first; then the date is confirmed
        return _ask_to_book_venue(event, day)
    event.pending_book = None
    attendees = [g.email for g in event.people if g.status != "declined"]
    google = get_google()
    if event.calendar_event_id:  # rescheduling: move the existing event, Google emails the update
        event.calendar_link = google.move_event(
            event.calendar_event_id, day, event.timezone, attendees,
            start_time=event.start_time, duration_minutes=event.duration_minutes,
        ) or event.calendar_link
        verb = f"Moved the calendar event to {chat.nice(day)}"
    else:
        event.calendar_event_id, event.calendar_link = google.create_event(
            title=event.title, day=day, tz_name=event.timezone, attendees=attendees,
            description=event.description, location=_location(event),
            start_time=event.start_time, duration_minutes=event.duration_minutes,
        )
        verb = f"Booked {chat.nice(day)}"
    event.status, event.booked_date, event.calendar_date = "booked", day, day
    event.log("system", f"{verb}. Google Calendar sent everyone the invite.")
    chat.notice(event.id, f"{verb} at {chat.nice_time(event.start_time)}. Google Calendar sent your guests the invite.")

    note = (f"The organizer booked {day}. Google Calendar has sent the invite or update to "
            f"{', '.join(attendees)}. Send each attending guest a short confirmation.")
    if event.restaurant:
        _check_resy(event, day)
        note += (f" Also email {event.restaurant.name} ({event.restaurant.email}) to confirm the party: "
                 f"{event.headcount} people on {day} at {event.start_time}.")
    # A picnic area is already booked by now (the gate above), so there's nothing to recheck.
    if needs_venue(event):
        note += (" The organizer is going out but still hasn't picked a venue: call offer_venues so they can pick "
                 "one for this date.")
    return note


# ---------------- venues ----------------


def _restaurant_slug(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())[:20] or "restaurant"


def _apply_venue(event: Event, venue_id: str) -> str | None:
    v = venues.by_id(venue_id)
    if not v or event.setting != "out" or event.status in ("stopped", "done"):
        return None
    if event.venue and event.venue.get("id") == venue_id:
        return None
    old = event.venue
    old_restaurant = event.restaurant
    if old_restaurant:
        old_restaurant.status = "declined"  # no longer the venue; kept for the email history
    event.picnic_alternatives = []
    day = event.booked_date if event.date_mode == "specific" else event.pending_book
    switched = f" (switching from {old['name']})" if old else ""

    if venue_id.startswith("resy-"):
        from personas import plus_address

        g = Guest(
            email=plus_address(event.organizer_email, _restaurant_slug(v["name"]), {x.email for x in event.guests}),
            name=v["name"], kind="restaurant", real_email=v["email"], persona="restaurant",
        )
        event.guests.append(g)
        event.venue = {"type": "restaurant", "id": v["id"], "name": v["name"], "photo": v.get("photo", ""),
                       "url": v.get("resy_url", ""), "address": v.get("address", ""), "token": g.token}
        event.log("organizer", f"Chose {v['name']} as the venue{switched}")
        if day:
            _check_resy(event, day)
        party = f"about {event.headcount} people"
        ask = (f"whether they can host {party} on {day} at {event.start_time}" if day else
               f"which dates between {event.window_start} and {event.window_end} they can host {party} at "
               f"{event.start_time} ({event.time_of_day})")
        old_note = f" The previous venue ({old['name']}) is no longer needed." if old else ""
        return (f"The organizer chose the restaurant {v['name']} ({v.get('neighborhood', '')}) as the venue{switched}. "
                f"Email them now at {g.email} asking {ask}.{old_note}")

    # A picnic area: the app checks availability and links to SF Rec & Park's booking page.
    event.venue = {"type": "picnic", "id": v["id"], "name": f"{v['name']}, {v['park']}", "photo": v.get("photo", ""),
                   "url": v["book_url"], "address": v.get("address", ""), "booked": False,
                   "facility_id": v["facility_id"]}
    event.log("organizer", f"Chose {v['name']} ({v['park']}) as the venue{switched}")
    if day:
        _check_picnic(event, day)
    else:
        chat.say(event.id, f"Great choice: {v['park']}. Once we've settled on a date, I'll check it's free and give "
                           "you the booking link.")
    if not (old and old["type"] == "picnic"):
        chat.offer_shopping(event, f"a picnic at {v['park']}")
    return (f"The organizer chose a picnic area as the venue{switched}: {v['name']} in {v['park']}. The app handles "
            "its availability check and booking link; nothing to email about it."
            + (f" The restaurant {old_restaurant.name} is no longer needed." if old_restaurant else ""))


def _check_resy(event: Event, day: str) -> None:
    r = event.restaurant
    v = venues.by_id(event.venue["id"]) if event.venue else None
    if not (r and v and v.get("resy_id")):
        return
    found = venues.resy_times(v["resy_id"], day, max(event.headcount, 2), event.start_time)
    if found["ok"]:
        summary = (f"Resy shows {', '.join(found['times'])} open for {event.headcount} on {chat.nice(day)}"
                   if found["times"] else f"Resy shows no online tables for {event.headcount} on {chat.nice(day)} "
                                          "(groups often book by email instead)")
        event.log("lookup", f"Checked {r.name} on Resy for {event.headcount} on {day}: "
                            f"{len(found['times'])} slots ({found['seconds']}s)")
    else:
        summary = "Couldn't check Resy just now"
        event.log("lookup", f"Resy check for {r.name} failed ({found['seconds']}s): {found['error']}")
    event.venue["check"] = {"kind": "resy", "day": day, "times": found["times"], "summary": summary}


def _check_picnic(event: Event, day: str) -> None:
    v = venues.by_id(event.venue["id"])
    if not v:
        return
    result = venues.check_picnic(v, day, event.time_of_day, event.location, event.headcount)
    status = result["status"]
    event.log("lookup", f"Checked {v['name']} ({v['park']}) on SF Rec & Park for {day}: {status} "
                        f"({result['seconds']}s)" + (f" {result['error']}" if result["error"] else ""))
    when = f"{chat.nice(day)} ({'morning' if event.time_of_day == 'morning' else 'afternoon'})"
    if status == "free":
        summary = f"Free on {when}"
        chat.say(event.id, f"{v['name']} at {v['park']} is free on {when}. Book it on SF Rec & Park's site, "
                           "then let me know." + (" I'll confirm the date with everyone once it's booked."
                                                  if event.pending_book == day else ""),
                 _booking_widget(event, day))
    elif status == "taken":
        summary = f"Taken on {when}"
        event.picnic_alternatives = result["alternatives"]
        if result["alternatives"]:
            chat.offer_venues(event, f"{v['name']} at {v['park']} is already taken on {when}. These nearby "
                                     "picnic areas are free:")
        else:
            chat.say(event.id, f"{v['name']} at {v['park']} is already taken on {when}, and I couldn't find a free "
                               "one nearby. You could pick a restaurant instead.")
    elif status == "not_bookable_yet":
        summary = result["note"]
        chat.say(event.id, f"{v['name']} can't be booked yet: {result['note']} Here's the page for when it opens.",
                 _booking_widget(event, day))
    else:
        summary = "Couldn't check availability just now"
        chat.say(event.id, f"I couldn't reach SF Rec & Park to check {v['name']} just now. You can check and book "
                           "it on their site.", _booking_widget(event, day))
    event.venue["check"] = {"kind": "picnic", "day": day, "status": status, "summary": summary}
    event.venue.setdefault("dates", {})[day] = status


def _booking_widget(event: Event, day: str) -> dict:
    v = event.venue
    restaurant = v["type"] == "restaurant"
    return {"type": "venue_booked", "url": v.get("url", ""), "name": v["name"], "day": day,
            "link_label": "Reserve on Resy" if restaurant else "Book on SF Rec & Park",
            "phone": (venues.by_id(v["id"]) or {}).get("phone", "") if restaurant else ""}


def _ask_to_book_venue(event: Event, day: str) -> str:
    """Before a date is confirmed with everyone, the venue gets booked for it."""
    when = chat.nice(day)
    if not event.venue:
        chat.offer_venues(event, f"Before I confirm {when} with everyone, let's lock in the venue. Pick one and "
                                 "I'll check it's free that day.")
        return (f"The organizer chose {day}, but no venue is picked yet. The app asked them to pick one and will "
                "confirm the date once the venue is booked. Don't email guests about the date yet.")
    if event.venue["type"] == "picnic":
        chat.say(event.id, f"Before I confirm {when} with everyone, let's make sure the picnic area is booked.")
        _check_picnic(event, day)
        return (f"The organizer chose {day}. The app is getting the picnic area booked first; the date is confirmed "
                "once it is. Don't email guests about the date yet.")
    r = event.restaurant
    if r and r.status == "responded" and day in r.available_dates:
        _prompt_restaurant_booking(event, day)
        return (f"The organizer chose {day}. {r.name} said they can host; the app asked the organizer to book the "
                "table, and the date is confirmed once they have. Don't email guests about the date yet.")
    return (f"The organizer chose {day}, but {event.venue['name']} hasn't said they can host that day. Email them "
            f"({r.email if r else ''}) to ask about {day}. Don't email guests about the date yet.")


def _prompt_restaurant_booking(event: Event, day: str) -> None:
    v = event.venue
    v["prompted_for"] = day
    chat.say(event.id, f"{v['name']} can host you on {chat.nice(day)}. Book the table with them (on Resy or by "
                       "phone), then let me know" + (" and I'll confirm the date with everyone."
                                                     if event.pending_book == day else "."),
             _booking_widget(event, day))


def _maybe_prompt_restaurant_booking(event: Event) -> None:
    """After a restaurant replies: if it can host on the date we need, ask the organizer to book."""
    v, r = event.venue, event.restaurant
    day = event.pending_book or (event.booked_date if event.date_mode == "specific" else None)
    if not (v and r and day) or v.get("booked") and v.get("booked_for") == day:
        return
    if r.status == "responded" and day in r.available_dates and v.get("prompted_for") != day:
        _prompt_restaurant_booking(event, day)


def _apply_venue_booked(event: Event, booked: bool, day: str | None, get_google) -> str | None:
    if not event.venue:
        return None
    day = day or event.pending_book or event.booked_date
    if not booked:
        chat.say(event.id, "No problem. The booking link is at the top of the Availability tab for when you're ready.")
        return None
    event.venue["booked"], event.venue["booked_for"] = True, day
    event.log("organizer", f"Booked {event.venue['name']}" + (f" for {day}" if day else ""))
    if event.pending_book and event.pending_book == day:
        chat.say(event.id, f"{event.venue['name']} is booked. Confirming {chat.nice(day)} with everyone now.")
        return _book(event, day, get_google)
    chat.say(event.id, f"{event.venue['name']} is booked.")
    return None


# ---------------- moving the date ----------------


def _reschedule(event: Event, pick: dict, get_google) -> str | None:
    """The organizer picked a new date (a specific day or a window). Moves the event and
    resets everyone's answers; the agent then tells everyone."""
    if event.status not in ("collecting", "proposed", "booked"):
        return None
    old = event.booked_date or event.calendar_date
    if old and old not in event.previous_dates:
        event.previous_dates.append(old)
    event.round += 1
    event.proposal, event.pending_question = None, None
    if pick.get("mode") == "specific":
        day = pick["date"]
        event.date_mode, event.window_bucket = "specific", 0
        event.window_start = event.window_end = day
        event.status, event.booked_date = "booked", day
        what = f"the specific date {day} ({date.fromisoformat(day):%A})"
    else:
        event.date_mode, event.window_bucket = "window", pick["bucket"]
        event.window_start, event.window_end = window_dates_for(pick["bucket"])
        event.status, event.booked_date = "collecting", None
        what = f"a new window, {event.window_start} to {event.window_end}"
    for g in event.attending:
        g.status, g.available_dates, g.reminders_sent = "invited", [], 0
    event.pending_book = None
    if event.venue and event.venue["type"] == "restaurant":
        event.venue.update(booked=False, booked_for=None, prompted_for=None)
    event.log("system", f"Moving the date to {what}.")

    picnic = ""
    if event.venue and event.venue["type"] == "picnic":
        was_booked = event.venue.get("booked")
        event.venue["booked"] = False
        event.venue.pop("check", None)
        if was_booked and old:
            chat.say(event.id, f"Remember to cancel your picnic booking for {chat.nice(old)} on SF Rec & Park.",
                     {"type": "link", "url": event.venue["url"], "label": "Open SF Rec & Park →"})
        if event.date_mode == "specific":
            _check_picnic(event, event.booked_date)
        picnic = " The picnic area is handled by the app."
    restaurant = ""
    if event.restaurant:
        restaurant = (f" Also email the restaurant {event.restaurant.name} ({event.restaurant.email}) that the date "
                      f"is moving and ask {'whether they can host on the new date' if event.date_mode == 'specific' else 'which new dates work'}.")
        if event.date_mode == "specific":
            _check_resy(event, event.booked_date)
    return (f"The organizer moved the date to {what}. Everyone's answers are reset. Email every attending guest that "
            f"the date is moving and ask {'whether they can make the new date' if event.date_mode == 'specific' else 'which dates in the new window work'}."
            f"{restaurant}{picnic} The calendar event moves once the new date is settled.")


# ---------------- grocery delivery ----------------


def _order_at(event: Event, day: str) -> datetime:
    """When to place the order: the morning a couple of days before, or now if that's passed."""
    tz = ZoneInfo(event.timezone)
    at = datetime.combine(date.fromisoformat(day) - timedelta(days=ORDER_DAYS_BEFORE), datetime.min.time(), tz)
    return max(at.replace(hour=9), now_utc())


def delivery_due(event: Event) -> bool:
    d = (db.get_shopping(event.id) or {}).get("delivery")
    if not d:
        return False
    if d["status"] == "cancelled":
        return bool(d.get("reminder_id"))
    if event.booked_date and (d.get("date") != event.booked_date or d.get("reminder_slot") != d["slot"]):
        return True
    return d["status"] == "scheduled" and bool(d.get("order_at")) and parse_iso(d["order_at"]) <= now_utc()


def _set_delivery(event: Event, **changes) -> None:
    db.update_shopping(event.id, lambda x: {**x, "delivery": {**x["delivery"], **changes}} if x and x.get("delivery") else None)


def _delivery(event: Event, get_google) -> None:
    """Keeps a scheduled grocery delivery in step with the shower: the calendar reminder on
    the day, and asking for payment when it's time to order."""
    import shopping

    data = db.get_shopping(event.id) or {}
    d = data.get("delivery")
    if not d:
        return
    if d["status"] == "cancelled":
        if d.get("reminder_id"):
            get_google().delete_event(d["reminder_id"])
            _set_delivery(event, reminder_id=None, reminder_slot=None)
            event.log("system", "Grocery delivery cancelled; removed the calendar reminder.")
            chat.notice(event.id, "Grocery delivery cancelled. I removed the reminder from your calendar.")
        return
    day = event.booked_date
    if not day or event.status not in ("booked",):
        return  # scheduled for "the morning of the shower": waits until the date is set
    label = shopping.slot_label(d["slot"])
    if d.get("date") != day or d.get("reminder_slot") != d["slot"]:
        order_at = _order_at(event, day)
        items = [f"{it['quantity']} x {it['display_name']}" for it in data.get("items", []) if not it.get("removed")]
        where = (data.get("store") or {}).get("name", "Kroger")
        start = d["slot"][:5]
        google = get_google()
        if d.get("reminder_id"):
            google.move_event(d["reminder_id"], day, event.timezone, [], start_time=start, duration_minutes=120)
        else:
            reminder_id, _ = google.create_event(
                title="Grocery delivery for the baby shower", day=day, tz_name=event.timezone, attendees=[],
                description=f"Delivery from {where}, {label}.\n\n" + "\n".join(items),
                location=(event.location or {}).get("postal", ""), start_time=start, duration_minutes=120)
            d["reminder_id"] = reminder_id
        reordered = d["status"] == "ordered" and d.get("date") not in (None, day)
        _set_delivery(event, date=day, order_at=order_at.isoformat(), reminder_id=d["reminder_id"],
                      reminder_slot=d["slot"], **({"status": "scheduled"} if reordered else {}))
        local = order_at.astimezone(ZoneInfo(event.timezone))
        event.log("system", f"Grocery delivery set for {day}, {label}; ordering on {local:%Y-%m-%d %H:%M}.")
        chat.notice(event.id, f"Delivery scheduled for {chat.nice(day)}, {label}. I'll check in on "
                              f"{chat.nice(local.date().isoformat())} to place the order, and I added a reminder "
                              "to your Google Calendar for the delivery.")
        if reordered:
            chat.say(event.id, "The groceries were already ordered for the old date, so I'll place a new order "
                               "for the new date when it's time.")
        d = (db.get_shopping(event.id) or {}).get("delivery") or d
    if d["status"] == "scheduled" and d.get("order_at") and parse_iso(d["order_at"]) <= now_utc():
        total = shopping.totals(data)["total"]
        _set_delivery(event, status="awaiting_payment")
        event.log("action", "Time to order the groceries: asked the organizer to confirm payment")
        chat.say(event.id, f"It's time to order the groceries for {chat.nice(day)}. The list comes to about "
                           f"${total:,.2f}, delivered {label}. Confirm a card and I'll place the order.",
                 {"type": "payment", "total": total, "label": label})


def _apply_payment(event: Event, ok: bool) -> None:
    import shopping

    data = db.get_shopping(event.id) or {}
    d = data.get("delivery")
    if not d or d["status"] != "awaiting_payment":
        return
    label = shopping.slot_label(d["slot"])
    if ok:
        _set_delivery(event, status="ordered", ordered_at=now_utc().isoformat())
        where = (data.get("store") or {}).get("name", "Kroger")
        event.log("action", f"Placed the grocery order with {where} (demo: nothing charged)")
        chat.notice(event.id, f"Groceries ordered from {where}, arriving {chat.nice(d['date'])}, {label}. "
                              "(Demo: no card was charged and no real order was placed.)")
    else:
        later = now_utc() + PAYMENT_RETRY[event.demo]
        _set_delivery(event, status="scheduled", order_at=later.isoformat())
        chat.say(event.id, "No problem. I'll check back " + ("in a couple of minutes." if event.demo else "tomorrow."))


def _on_sent_hook():
    import personas  # simulated replies: demo guests, and restaurants (always simulated)

    return personas.on_sent
