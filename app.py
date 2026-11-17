"""Web app: one page per baby shower, with the chat on the left and venues, availability,
shopping list, emails and the agent's activity on the right.

    uv run uvicorn app:app --port 8000      (then open http://localhost:8000)

Visitors can chat before connecting Gmail: the draft event belongs to their browser
session until they connect, then to their Google account.
"""
from __future__ import annotations

import secrets
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import agent
import chat
import config
import db
import google_api
import inbound
import personas
import shopping
import venues
import worker
from models import Event, Guest, now_utc, parse_iso

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, same_site="lax",
                   https_only=config.BASE_URL.startswith("https"))
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.filters["nice_date"] = lambda d: chat.nice(d) if d else ""
templates.env.filters["long_date"] = lambda d: f"{date.fromisoformat(d):%A, %B} {date.fromisoformat(d).day}" if d else ""
templates.env.filters["clock"] = lambda ts, tz: f"{parse_iso(ts).astimezone(ZoneInfo(tz)):%H:%M:%S}" if ts else ""
templates.env.filters["money"] = lambda x: f"${x:,.2f}" if x else ""
templates.env.filters["nice_time"] = chat.nice_time
# Park amenities worth showing on a baby shower card (the city also lists permits like "Alcohol").
templates.env.filters["amenities"] = lambda xs: [a for a in xs or [] if a.lower() not in ("alcohol", "jumper", "amplified sound")]

ERRORS = {
    "denied": "No problem. I need Gmail access to email your guests, so connect whenever you're ready.",
    "permissions": "Please leave all the boxes ticked on Google's screen. I need to send email and add the event to your calendar.",
    "expired": "That sign-in took too long. Please try again.",
}

_setup_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def current_user(request: Request) -> dict | None:
    uid = request.session.get("user_id")
    return db.get_user(uid) if uid else None


def owned_event(request: Request, event_id: str) -> Event:
    """The event, if this visitor may see it: their own, or their browser's draft."""
    event = db.load_event(event_id)
    if event:
        user = current_user(request)
        if event.user_id and user and event.user_id == user["id"]:
            return event
        if not event.user_id and event.id in request.session.get("drafts", []):
            return event
    raise HTTPException(404)


def new_draft(request: Request) -> Event:
    user = current_user(request)
    event = Event(id=secrets.token_urlsafe(6), user_id=user["id"] if user else "")
    if user:
        event.organizer_name, event.organizer_email = user["name"] or user["email"].split("@")[0], user["email"]
    db.create_event(event)
    if not user:
        request.session["drafts"] = (request.session.get("drafts", []) + [event.id])[-5:]
    chat.advance(event, user)
    db.save_event(event)
    return event


# ---------------- pages ----------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, notice: str = ""):
    """The entry point: a mock registry dashboard (all dummy content) with a button that
    opens a modal pitching the agent."""
    return templates.TemplateResponse(request, "dashboard.html", {"notice": notice == "reset"})


@app.get("/start")
def start(request: Request):
    """"Try it now": into the chat. Picks up a shower still being set up, else starts one."""
    user = current_user(request)
    if user:
        drafts = [e for e in db.list_events(user["id"]) if e.status == "setup"]
    else:
        drafts = [e for e in (db.load_event(i) for i in request.session.get("drafts", [])) if e and not e.user_id]
    event = drafts[0] if user and drafts else drafts[-1] if drafts else new_draft(request)
    return see_other(f"/events/{event.id}")


@app.get("/showers", response_class=HTMLResponse)
def showers(request: Request):
    user = current_user(request)
    if not user:
        return see_other("/start")
    return templates.TemplateResponse(request, "home.html", {"user": user, "events": db.list_events(user["id"])})


@app.post("/events/new")
@app.get("/events/new")
def create_draft(request: Request):
    return see_other(f"/events/{new_draft(request).id}")


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: str, error: str = "", notice: str = ""):
    event = owned_event(request, event_id)
    user = current_user(request)
    return templates.TemplateResponse(request, "event.html", {
        "user": user, "event": event,
        "error": ERRORS.get(error),
        "notice": "Demo reset. Everything is gone and Gmail access has been removed." if notice == "reset" else None,
        "sample_guests": personas.SAMPLE_GUESTS,
        "my_email": user["email"] if user else "",
        **_panel_context(event),
    })


def _panel_context(event: Event) -> dict:
    near = venues.nearest(event.location)
    data = db.get_shopping(event.id)
    return {
        "event": event,
        "near": near,
        "alternatives": [v for v in (venues.by_id(i) for i in event.picnic_alternatives) if v],
        "outside_sf": bool(event.location) and not event.location.get("in_sf"),
        "summary": event.date_summary(),
        "dates": event.grid_dates(),
        "all_dates": len(event.window_dates()),
        "chosen": event.booked_date or (event.proposal or {}).get("date"),
        "shop": data,
        "shop_totals": shopping.totals(data) if data else None,
        "activity": list(reversed(event.activity[-120:])),
        "can_choose_venue": event.setting == "out" and event.status in ("collecting", "proposed", "booked"),
        "invitees": _invitees(event),
        "shopping_offer": _open_widget(event.id, "shopping_offer"),
        "delivery_slots": [(s, shopping.slot_label(s)) for s in shopping.delivery_slots(event.start_time)],
        "slot_label": shopping.slot_label,
        "delivery": (data or {}).get("delivery"),
        "status_of": lambda g: chat.person_status(event, g),
    }


def _open_widget(event_id: str, kind: str) -> dict | None:
    return next((m for m in reversed(db.list_chat(event_id))
                 if (m["widget"] or {}).get("type") == kind and not m["answered"]), None)


def _invitees(event: Event) -> list:
    """Guests (host and key people first), then the restaurant(s) emailed."""
    people = sorted(event.people, key=lambda g: (g.role != "host", not g.key))
    return people + [g for g in event.guests if g.is_restaurant and (g.transcript or g is event.restaurant)]


def _with_cards(event: Event, m: dict) -> dict:
    """The intro's venue cards depend on where the visitor is, so they're filled in live."""
    if (m["widget"] or {}).get("type") == "inspiration":
        m["widget"] = {**m["widget"], "cards": chat.inspiration(event)}
    return m


@app.get("/events/{event_id}/state")
def event_state(request: Request, event_id: str):
    """Polled every ~1.5s by the page."""
    event = owned_event(request, event_id)
    ctx = _panel_context(event)
    pending = db.pending_inbox(event.id)
    return {
        "status": event.status,
        "demo": event.demo,
        "thinking": event.status != "setup" and (worker.is_busy(event.id) or any(
            i["kind"] in ("organizer_note", "activated", "answer", "new_date", "venue") for i in pending)),
        "chat": [_with_cards(event, {**{k: m[k] for k in ("id", "role", "kind", "text", "widget", "answered")},
                                     "age": (now_utc() - parse_iso(m["created_at"])).total_seconds()})
                 for m in db.list_chat(event.id)],
        # The right column appears once there's something to show: the agent has started.
        "side": event.status != "setup",
        "people": {g.token: dict(zip(("color", "label"), chat.person_status(event, g))) for g in event.guests},
        "panel": templates.get_template("_panel.html").render(**ctx),
        "top": templates.get_template("_top.html").render(
            event=event, order_waiting=((ctx["delivery"] or {}).get("status") == "scheduled"
                                        and bool((ctx["delivery"] or {}).get("date")))),
    }


# ---------------- the chat ----------------


@app.get("/events/{event_id}/venues", response_class=HTMLResponse)
def all_venues(request: Request, event_id: str, kind: str = "restaurants"):
    """The "View all" panel: every saved venue of one kind, with live availability."""
    event = owned_event(request, event_id)
    kind = "picnic" if kind == "picnic" else "restaurants"
    day = event.booked_date or (event.window_start if event.date_mode == "specific" else None)
    party = event.headcount if event.people else 10
    items = venues.all_venues(kind, event.location, day, event.time_of_day or "afternoon", party, event.start_time)
    return templates.TemplateResponse(request, "_venues.html", {
        "kind": kind, "items": items, "day": day, "party": party, "maps_url": chat.maps_url,
        "can_choose": event.setting == "out" and event.status in ("collecting", "proposed", "booked"),
        "current": (event.venue or {}).get("id"), "status": event.status, "setting": event.setting,
        "place": (event.location or {}).get("city") if (event.location or {}).get("in_sf") else "San Francisco",
    })


@app.post("/events/{event_id}/choose-venue")
async def choose_venue(request: Request, event_id: str):
    """"Choose this venue" in the View all panel. Answers the chat's open venue question
    if there is one; either way the worker takes it from there."""
    event = owned_event(request, event_id)
    v = venues.by_id(str((await request.json()).get("venue_id")))
    if not v or event.setting != "out" or event.status not in ("collecting", "proposed", "booked"):
        return JSONResponse({"error": "You can pick a venue once the agent has started, if you're going out."},
                            status_code=400)
    if event.venue and event.venue.get("id") == v["id"]:
        return {"ok": True}
    open_q = _open_widget(event_id, "venue")
    if open_q:
        db.answer_widget(open_q["id"])
    name = v["name"] if v["id"].startswith("resy") else f"{v['name']}, {v['park']}"
    chat.user_said(event_id, f"Let's go with {name}")
    db.add_inbox(event_id, "venue", {"venue_id": v["id"]})
    worker.wake(event_id)
    return {"ok": True}


@app.post("/events/{event_id}/locate")
async def locate(request: Request, event_id: str):
    """The page reports the visitor's approximate location (from their IP) and timezone."""
    event = owned_event(request, event_id)
    data = await request.json()
    with _setup_locks[event_id]:
        event = db.load_event(event_id)
        if event.status != "setup":
            return {"ok": True}
        try:
            lat, lng = float(data.get("lat")), float(data.get("lng"))
        except (TypeError, ValueError):
            lat = lng = None
        sf = venues.in_sf(lat, lng)
        city = str(data.get("city") or "")[:60]
        event.location = {
            "city": city if sf else "San Francisco", "region": str(data.get("region") or "")[:30],
            "postal": str(data.get("postal") or "")[:10] if sf else config.DEFAULT_ZIP,
            "lat": lat if sf else config.SF_CENTER[0], "lng": lng if sf else config.SF_CENTER[1],
            "in_sf": sf, "detected": city or "unknown",
        }
        event.area = event.location["city"]
        try:
            tz = str(data.get("timezone") or "")
            ZoneInfo(tz)
            event.timezone = tz
        except (ZoneInfoNotFoundError, ValueError):
            pass
        db.save_event(event)
    return {"ok": True}


@app.post("/events/{event_id}/reask")
async def reask(request: Request, event_id: str):
    """Setup: the organizer tapped an earlier answer to change it."""
    owned_event(request, event_id)
    step = (await request.json()).get("step")
    with _setup_locks[event_id]:
        event = db.load_event(event_id)
        if event.status == "setup" and step in ("host", "setting", "date", "time") and step in event.setup_done:
            db.close_open_widgets(event_id)
            chat.present(event, step, current_user(request))
    return {"ok": True}


@app.post("/events/{event_id}/widget")
async def answer_widget(request: Request, event_id: str):
    owned_event(request, event_id)
    data = await request.json()
    msg = db.get_chat(int(data.get("message_id") or 0))
    if not msg or msg["event_id"] != event_id or not msg["widget"] or msg["answered"]:
        return JSONResponse({"error": "That question has already been answered."}, status_code=409)
    widget, value, user = msg["widget"], data.get("value"), current_user(request)

    with _setup_locks[event_id]:
        event = db.load_event(event_id)
        if event.status == "setup":
            return _answer_setup(event, msg, widget, value, user)

    kind = widget["type"]
    if kind == "venue":
        v = venues.by_id(str(value))
        if not v or not any(o["id"] == v["id"] for o in widget["options"]):
            return JSONResponse({"error": "Pick one of these venues."}, status_code=400)
        bubble, inbox = v["name"], ("venue", {"venue_id": v["id"]})
    elif kind == "shopping_offer":
        if value == "yes":
            _start_shopping(event)
        bubble, inbox = ("Yes, make the list" if value == "yes" else "No thanks"), None
    elif kind == "question":
        if value not in ("approve", "decline"):
            return JSONResponse({"error": "Pick one."}, status_code=400)
        bubble = widget["approve_label" if value == "approve" else "decline_label"]
        inbox = ("answer", {"question_id": widget["question_id"], "choice": value})
    elif kind == "proposal":
        choices = [widget["date"]] + [a["date"] for a in widget.get("alternatives", [])]
        if value not in choices:
            return JSONResponse({"error": "Pick one of these dates."}, status_code=400)
        bubble, inbox = f"Book {chat.nice(value)}", ("book", {"date": value})
    elif kind == "new_date":
        pick = _parse_date_pick(value)
        if not pick:
            return JSONResponse({"error": "Pick a date after today, or one of the windows."}, status_code=400)
        bubble, inbox = pick.pop("label"), ("new_date", pick)
    elif kind == "payment":
        bubble = "Use my card ending 4242" if value == "yes" else "Not yet"
        inbox = ("payment", {"ok": value == "yes"})
    elif kind in ("venue_booked", "picnic_booked"):
        bubble = "I booked it" if value == "yes" else "Not yet"
        inbox = ("venue_booked", {"booked": value == "yes", "day": widget.get("day")})
    else:
        return JSONResponse({"error": "Nothing to answer."}, status_code=400)

    if not db.answer_widget(msg["id"]):
        return JSONResponse({"error": "That question has already been answered."}, status_code=409)
    chat.user_said(event_id, bubble)
    if inbox:
        db.add_inbox(event_id, *inbox)
        worker.wake(event_id)
    return {"ok": True}


def _parse_date_pick(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    if value.get("mode") == "specific":
        try:
            d = date.fromisoformat(str(value.get("date")))
        except ValueError:
            return None
        if d <= date.today() or (d - date.today()).days > 365:
            return None
        return {"mode": "specific", "date": d.isoformat(), "label": f"Move it to {chat.nice(d.isoformat())}"}
    if value.get("mode") == "window" and value.get("bucket") in (1, 2, 3):
        from models import window_dates_for

        s, e = window_dates_for(value["bucket"])
        return {"mode": "window", "bucket": value["bucket"], "label": f"Find a new date, {chat.nice(s)} – {chat.nice(e)}"}
    return None


def _answer_setup(event: Event, msg: dict, widget: dict, value, user: dict | None):
    kind = widget["type"]
    if kind == "guests":
        return _activate(event, msg, value, user)
    step = {"host": "host", "date": "date"}.get(kind) or widget.get("step")
    text = chat.apply_setup_answer(event, step, value) if step else None
    if text is None:
        return JSONResponse({"error": "Please pick one of the options."}, status_code=400)
    if not db.answer_widget(msg["id"]):
        return JSONResponse({"error": "That question has already been answered."}, status_code=409)
    chat.user_said(event.id, text)
    chat.advance(event, user)
    db.save_event(event)
    return {"ok": True}


def _activate(event: Event, msg: dict, value, user: dict | None):
    """The guest list is in: the agent takes over."""
    if not user:
        return JSONResponse({"error": "Connect Gmail first."}, status_code=401)
    value = value if isinstance(value, dict) else {}
    demo = bool(value.get("demo"))
    guests: list[Guest] = []
    for i, raw in enumerate(r for r in value.get("guests") or [] if isinstance(r, dict)):
        name = str(raw.get("name", "")).strip()[:80]
        email = parseaddr(str(raw.get("email", "")).strip())[1].lower()
        if demo:  # real names, simulated people: invites go to plus-addresses of your own Gmail
            name = name or (email.split("@")[0] if email else f"Guest {i + 1}")
            email = personas.plus_address(user["email"], name, {g.email for g in guests})
            persona = personas.persona_for(i)
        else:
            if "@" not in email or "." not in email.rpartition("@")[2]:
                return JSONResponse({"error": f"{name or 'A guest'} needs a valid email address."}, status_code=400)
            persona = ""
        if any(g.email == email for g in guests):
            continue
        guests.append(Guest(email=email, name=name, persona=persona, key=bool(raw.get("key"))))
    if not guests:
        return JSONResponse({"error": "Add at least one guest."}, status_code=400)
    if demo and not any(g.key for g in guests):
        guests[0].key = True
    if event.host_name:  # someone else is hosting: they're invited too, and the date has to work for them
        host_email = (personas.plus_address(user["email"], event.host_name, {g.email for g in guests})
                      if demo else event.host_email)
        guests = [g for g in guests if g.email != host_email]
        guests.insert(0, Guest(email=host_email, name=event.host_name, key=True, role="host",
                               persona=personas.HOST_PERSONA if demo else ""))

    if not db.answer_widget(msg["id"]):
        return JSONResponse({"error": "That question has already been answered."}, status_code=409)
    organizer_first = (user["name"] or user["email"].split("@")[0]).split()[0]
    event.user_id, event.organizer_email = user["id"], user["email"]
    event.organizer_name = user["name"] or organizer_first
    event.title = f"Baby shower hosted by {(event.host_name or organizer_first).split()[0]}"
    slug = "-".join("".join(ch for ch in w if ch.isalnum()) for w in event.organizer_name.lower().split()) or "my"
    event.registry_url = config.REGISTRY_URL.format(slug=slug)
    event.guests, event.demo = guests, demo
    event.setup_done.append("guests")
    if event.date_mode == "specific":
        event.status, event.booked_date = "booked", event.window_start
    else:
        event.status = "collecting"
    event.log("system", "Setup finished. The agent is writing the invitations.")
    db.save_event(event)

    names = [g.name or g.email for g in guests]
    chat.user_said(event.id, ", ".join(names) if len(names) <= 4 else f"{', '.join(names[:3])} and {len(names) - 3} more")
    chat.say(event.id, "Got it! I'm writing the invitations now. Replies show up here, and the details (who can "
                       "make which day, the emails, and what I'm doing) are on the right."
                       + (" (Demo mode: the guests are simulated.)" if demo else ""))
    if event.setting == "out":
        chat.offer_venues(event, "While the invitations go out, pick a venue: restaurants that take groups of 10, "
                                 "or park picnic areas for 10+.")
    else:
        chat.offer_shopping(event, "your shower at home")
    db.add_inbox(event.id, "activated", {})
    worker.wake(event.id)
    return {"ok": True}


@app.post("/events/{event_id}/message")
async def message(request: Request, event_id: str):
    """The organizer typed something."""
    owned_event(request, event_id)
    text = str((await request.json()).get("text") or "").strip()[:config.MAX_MESSAGE_CHARS]
    if not text:
        return JSONResponse({"error": "Type a message."}, status_code=400)
    if db.count_user_messages(event_id) >= config.MAX_MESSAGES_PER_EVENT:
        return JSONResponse({"error": "That's the message limit for this demo. The buttons still work."},
                            status_code=429)
    user = current_user(request)
    chat.user_said(event_id, text, typed=True)

    with _setup_locks[event_id]:
        event = db.load_event(event_id)
        if event.status != "setup":
            if event.status in ("stopped", "done"):
                chat.notice(event_id, "The agent has finished with this event.")
            else:
                db.add_inbox(event_id, "organizer_note", {"text": text})
                worker.wake(event_id)
            return {"ok": True}

        step = chat.next_step(event, connected=bool(user))
        answers = agent.setup_reply(event, text, step)
        if answers is None:
            chat.say(event_id, "Sorry, I couldn't process that just now. You can use the options above.")
            return {"ok": True}
        applied = False
        if answers.host_is_me:
            applied |= chat.apply_setup_answer(event, "host", {"me": True}) is not None
        elif answers.host_name and answers.host_email:
            applied |= chat.apply_setup_answer(event, "host", {"name": answers.host_name,
                                                               "email": answers.host_email}) is not None
        if answers.setting:
            applied |= chat.apply_setup_answer(event, "setting", answers.setting) is not None
        if answers.specific_date:
            applied |= chat.apply_setup_answer(event, "date", {"mode": "specific", "date": answers.specific_date}) is not None
        elif answers.window:
            applied |= chat.apply_setup_answer(event, "date", {"mode": "window", "bucket": answers.window}) is not None
        if answers.time_of_day:
            applied |= chat.apply_setup_answer(event, "time", answers.time_of_day) is not None
        chat.say(event_id, answers.reply.strip()[:600] or "Got it.")
        if applied:
            db.close_open_widgets(event_id)
            chat.advance(event, user)
            db.save_event(event)
    return {"ok": True}


def _start_shopping(event: Event) -> None:
    where = "picnic" if event.venue and event.venue.get("type") == "picnic" else "home"
    details = (f"Hosted by {event.host_name or 'the organizer'}, in the {event.time_of_day or 'afternoon'}. "
               + (f"Notes: {event.description}" if event.description else ""))
    zip_code = (event.location or {}).get("postal") or config.DEFAULT_ZIP

    def log(text: str) -> None:  # the activity feed belongs to the worker, so go through the inbox
        db.add_inbox(event.id, "demo_log", {"text": text})
        worker.wake(event.id)

    shopping.start(event.id, where, event.headcount, details, zip_code, log)


@app.post("/events/{event_id}/make-list")
def make_list(request: Request, event_id: str):
    """The shopping tab's "Yes, make the list": answers the chat's offer the same way."""
    event = owned_event(request, event_id)
    offer = _open_widget(event_id, "shopping_offer")
    if not offer or db.get_shopping(event_id) or not db.answer_widget(offer["id"]):
        return JSONResponse({"error": "The shopping list is already on its way."}, status_code=409)
    chat.user_said(event_id, "Yes, make the list")
    _start_shopping(event)
    return {"ok": True}


@app.post("/events/{event_id}/shopping")
async def edit_shopping(request: Request, event_id: str):
    owned_event(request, event_id)
    data = await request.json()
    item = data.get("item")
    shopping.edit(event_id, str(data.get("action")), int(item) if isinstance(item, int) else None, data.get("value"))
    return {"ok": True}


@app.post("/events/{event_id}/picnic-booked")
def picnic_booked(request: Request, event_id: str):
    event = owned_event(request, event_id)
    if event.venue:
        for m in db.list_chat(event_id):
            if (m["widget"] or {}).get("type") in ("venue_booked", "picnic_booked") and not m["answered"]:
                db.answer_widget(m["id"])
        chat.user_said(event_id, "I booked it")
        db.add_inbox(event_id, "venue_booked", {"booked": True})
        worker.wake(event_id)
    return {"ok": True}


@app.post("/events/{event_id}/simulate-order-time")
def simulate_order_time(request: Request, event_id: str):
    """Demo: skip ahead to "a couple of days before the shower", when the groceries get ordered."""
    event = owned_event(request, event_id)
    d = (db.get_shopping(event_id) or {}).get("delivery")
    if event.demo and d and d["status"] == "scheduled" and d.get("date"):
        db.update_shopping(event_id, lambda x: {**x, "delivery": {**x["delivery"], "order_at": now_utc().isoformat()}})
        db.add_inbox(event_id, "demo_log", {"text": "Demo: skipped ahead to when the groceries get ordered."})
        worker.wake(event_id)
    return {"ok": True}


@app.post("/events/{event_id}/stop")
def stop(request: Request, event_id: str):
    event = owned_event(request, event_id)
    db.add_inbox(event.id, "stop", {})
    worker.wake(event.id)
    return {"ok": True}


@app.post("/events/{event_id}/simulate-cant-make-it")
async def simulate_cant_make_it(request: Request, event_id: str):
    """Demo: a key guest emails to say they can no longer make the booked date."""
    event = owned_event(request, event_id)
    guest = event.guest_by_token(str((await request.json()).get("guest", "")))
    if event.demo and event.status == "booked" and guest and guest.persona and guest.kind == "guest":
        db.add_inbox(event.id, "demo_log", {"text": f"Demo: {guest.first_name} is about to email that they can't make it."})
        worker.wake(event.id)
        personas.simulate_cant_make_it(event, guest)
    return {"ok": True}


# ---------------- Connect Gmail (Google sign-in) ----------------


@app.get("/auth/login")
def login(request: Request, event: str = ""):
    try:
        url, state, verifier = google_api.start_sign_in()
    except RuntimeError as e:  # Google client not configured yet
        return HTMLResponse(f"<p>{e}</p>", status_code=503)
    request.session["oauth_state"], request.session["oauth_verifier"] = state, verifier
    request.session["after_login"] = event
    return RedirectResponse(url)


@app.get("/auth/callback")
def auth_callback(request: Request):
    back = request.session.pop("after_login", "")
    back_url = f"/events/{back}" if back else "/"
    if request.query_params.get("error"):
        return see_other(f"{back_url}?error=denied")
    state, verifier = request.session.pop("oauth_state", None), request.session.pop("oauth_verifier", None)
    if not state or state != request.query_params.get("state"):
        return see_other(f"{back_url}?error=expired")
    try:
        user, token_json = google_api.finish_sign_in(str(request.url), state, verifier)
    except google_api.MissingPermissions:
        return see_other(f"{back_url}?error=permissions")
    db.save_user(user["id"], user["email"], user["name"], token_json)
    request.session["user_id"] = user["id"]

    with _setup_locks[back]:
        event = db.load_event(back) if back else None
        mine = (not event.user_id and back in request.session.get("drafts", [])) if event else False
        if event and event.status == "setup" and (mine or event.user_id == user["id"]):
            event.user_id = user["id"]
            event.organizer_email, event.organizer_name = user["email"], user["name"] or user["email"].split("@")[0]
            for m in db.list_chat(event.id):
                if m["widget"] and m["widget"].get("type") == "gmail" and not m["answered"]:
                    db.answer_widget(m["id"])
            chat.user_said(event.id, f"Connected {user['email']}")
            if "gmail" not in event.setup_done:
                event.setup_done.append("gmail")
            chat.advance(event, db.get_user(user["id"]))
            db.save_event(event)
    return see_other(back_url)


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return see_other("/")


@app.post("/reset")
def reset_demo(request: Request):
    """Start over: delete calendar events we created, revoke Google access, wipe this user's data."""
    user = current_user(request)
    if user:
        token = db.get_user_token(user["id"])
        if token:
            try:
                google = google_api.GoogleAccount(token)
                for event in db.list_events(user["id"]):
                    reminder = ((db.get_shopping(event.id) or {}).get("delivery") or {}).get("reminder_id")
                    for cal_id in (event.calendar_event_id, reminder):
                        if not cal_id:
                            continue
                        try:
                            google.delete_event(cal_id)
                        except Exception as e:  # already deleted by hand, etc.
                            print(f"[reset] couldn't delete calendar event: {e!r}")
            except Exception as e:
                print(f"[reset] calendar cleanup skipped: {e!r}")
            try:
                google_api.revoke(token)
            except Exception as e:
                print(f"[reset] revoke failed: {e!r}")
        db.delete_user_and_data(user["id"])
    drafts = [i for i in request.session.get("drafts", []) if (e := db.load_event(i)) and not e.user_id]
    db.delete_events(drafts)
    request.session.clear()
    return see_other("/?notice=reset")


# ---------------- guest autocomplete (Google Contacts) ----------------


def _contacts_account(request: Request):
    user = current_user(request)
    token = db.get_user_token(user["id"]) if user else None
    if not token:
        return None
    try:
        return google_api.GoogleAccount(token)
    except Exception as e:  # unreadable/expired token: autocomplete just switches off
        print(f"[contacts] can't open Google account: {e!r}")
        return None


@app.post("/api/contacts/warmup")
def contacts_warmup(request: Request):
    """Called when the guest step opens. Also tells the page whether contacts are available."""
    account = _contacts_account(request)
    if not account:
        return {"available": False, "reason": "no_account"}
    try:
        account.warm_up_contacts()
        return {"available": True, "suggested": account.suggested_contacts()}
    except Exception as e:
        text = str(e)
        if "has not been used in project" in text or "SERVICE_DISABLED" in text or "is disabled" in text:
            reason = "api_disabled"
            print("[contacts] The People API is disabled for this app's Google Cloud project. Enable it at "
                  "Google Cloud Console > APIs & Services > Library > People API (same project as GOOGLE_CLIENT_ID).")
        elif "insufficient" in text.lower() or "PERMISSION_DENIED" in text:
            reason = "no_permission"
        else:
            reason = "error"
            print(f"[contacts] unavailable: {e!r}")
        return {"available": False, "reason": reason}


@app.get("/api/contacts/search")
def contacts_search(request: Request, q: str = ""):
    account, q = _contacts_account(request), q.strip()
    if not account or len(q) < 1:
        return {"results": []}
    try:
        return {"results": account.search_contacts(q[:60])}
    except Exception as e:
        print(f"[contacts] search failed: {e!r}")
        return {"results": []}


# ---------------- SendGrid Inbound Parse webhook (production replies) ----------------


@app.post("/inbound/sendgrid")
async def sendgrid_inbound(request: Request, key: str = ""):
    if not secrets.compare_digest(key, config.INBOUND_SECRET):
        raise HTTPException(403)
    form = await request.form()
    _, status = inbound.receive({k: v for k, v in form.items() if isinstance(v, str)})
    return {"status": status}  # always 200 for valid calls, so SendGrid doesn't retry
