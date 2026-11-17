"""Tests: no network. Google, Resy, SF Rec & Park and Kroger are faked; the agent loop runs
against a mocked Anthropic API."""
import json
from datetime import date, datetime, timedelta, timezone

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

import agent
import app as webapp
import chat
import config
import db
import inbound
import personas
import shopping
import venues
import worker
from agent import AgentTools, ToolError
from models import Event, Guest

USER = {"id": "u1", "email": "stan@gmail.com", "name": "Stan Lee"}


class FakeGoogle:
    def __init__(self, *_):
        self.sent, self.created, self.moved, self.deleted = [], [], [], []

    def send_email(self, to, subject, body, reply_to=None, thread_id=None, in_reply_to=None, references=""):
        self.sent.append(dict(to=to, subject=subject, body=body, reply_to=reply_to))
        return f"m{len(self.sent)}", thread_id or f"t-{to}"

    def busy_by_date(self, start, end, tz):
        return {}

    def create_event(self, **kw):
        self.created.append(kw)
        return "cal-1", "https://calendar.google.com/event?x"

    def move_event(self, event_id, day, tz, attendees, start_time=None, duration_minutes=120):
        self.moved.append((event_id, day))
        return "https://calendar.google.com/event?x"

    def delete_event(self, event_id, notify=False):
        self.deleted.append(event_id)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init()
    db.save_user(USER["id"], USER["email"], USER["name"], "{}")
    # Never hit the network for live venue checks.
    monkeypatch.setattr(venues, "resy_times", lambda *a, **k: {"ok": True, "times": ["13:00", "13:30"], "seconds": 0.1, "error": ""})
    monkeypatch.setattr(venues, "picnic_search", lambda *a, **k: {"ok": True, "items": [], "seconds": 0.1, "error": ""})


def make_event(*names, demo=False, mode="window", days=7, eid="ev1") -> Event:
    start = date.today() + timedelta(days=7)
    end = start if mode == "specific" else start + timedelta(days=days - 1)
    ev = Event(
        id=eid, user_id=USER["id"], organizer_name=USER["name"], organizer_email=USER["email"],
        title="Sarah's baby shower", window_start=start.isoformat(), window_end=end.isoformat(),
        timezone="America/Los_Angeles", demo=demo, date_mode=mode, setting="home", time_of_day="afternoon",
        start_time="13:00", status="booked" if mode == "specific" else "collecting",
        booked_date=start.isoformat() if mode == "specific" else None,
        guests=[Guest(email=f"{n.lower()}@x.com", name=n) for n in names],
    )
    db.create_event(ev)
    return ev


def add_restaurant(ev: Event) -> Guest:
    r = Guest(email="stan+laughingmonk@gmail.com", name="Laughing Monk", kind="restaurant",
              real_email="nopa@laughingmonk.com", persona="restaurant")
    ev.guests.append(r)
    ev.setting = "out"
    ev.venue = {"type": "restaurant", "id": "resy-94120", "name": "Laughing Monk", "token": r.token}
    return r


def reply_form(ev: Event, name: str, text: str, sender: str | None = None) -> dict:
    g = ev.guest(f"{name.lower()}@x.com") or next(x for x in ev.guests if x.name == name)
    return inbound.to_sendgrid_form(sender or g.email, g.name, inbound.reply_address(g.token), "Re: hi", text)


def widgets(eid: str, kind: str | None = None, open_only=True) -> list[dict]:
    return [m for m in db.list_chat(eid) if m["widget"] and m["widget"].get("type")
            and (kind is None or m["widget"]["type"] == kind) and (not open_only or not m["answered"])]


# ---------------- date logic and guardrails ----------------


def test_date_summary_includes_restaurant():
    ev = make_event("Ana", "Ben", "Cy")
    d = ev.window_dates()
    ev.guests[0].status, ev.guests[0].available_dates = "responded", d[:4]
    ev.guests[1].status, ev.guests[1].available_dates = "responded", d[2:6]
    s = ev.date_summary()
    assert s["pending_responses"] == ["cy@x.com"] and s["dates_everyone_free"] == []
    ev.guests[2].status = "declined"
    assert ev.date_summary()["dates_everyone_free"] == d[2:4]
    r = add_restaurant(ev)
    assert ev.date_summary()["pending_responses"] == [r.email]  # the restaurant has a say
    r.status, r.available_dates = "responded", [d[3]]
    assert ev.date_summary()["dates_everyone_free"] == [d[3]]
    assert ev.headcount == 3  # Ana, Ben + organizer; not the restaurant or Cy


def test_send_guards():
    ev, g = make_event("Ana"), FakeGoogle()
    tools = AgentTools(ev, g)
    with pytest.raises(ToolError):
        tools.tool_send_email("stranger@evil.com", "hi")
    tools.tool_send_email("ana@x.com", "invite")
    assert g.sent[0]["reply_to"] == f"reply+{ev.guests[0].token}@{config.REPLY_DOMAIN}"
    assert g.sent[0]["subject"] == "Sarah's baby shower: finding a date"
    with pytest.raises(ToolError, match="Wait"):  # no reply yet, too soon to chase
        AgentTools(ev, g).tool_send_email("ana@x.com", "nudge")
    ev.guests[0].last_reply_at = datetime.now(timezone.utc).isoformat()
    AgentTools(ev, g).tool_send_email("ana@x.com", "thanks, which Friday?")  # they replied: fine
    assert g.sent[-1]["subject"].startswith("Re: ")
    assert "Invitations sent to Ana" in [m["text"] for m in db.list_chat(ev.id)]


def test_propose_requires_everyone():
    ev = make_event("Ana", "Ben")
    tools, d = AgentTools(ev, FakeGoogle()), ev.window_dates()
    tools.tool_record_availability("ana@x.com", d[:3] + ["1999-01-01"], "")
    with pytest.raises(ToolError, match="waiting"):
        tools.tool_propose_date(d[0], "x", [])
    tools.tool_record_availability("ben@x.com", d[2:], "prefers weekends")
    with pytest.raises(ToolError, match="doesn't work"):
        tools.tool_propose_date(d[0], "x", [])
    tools.tool_propose_date(d[2], "Only day both can do.", [d[0], d[2]])
    assert ev.status == "proposed" and ev.proposal["alternatives"] == []
    w = widgets(ev.id, "proposal")[0]["widget"]
    assert w["date"] == d[2]


def test_invitations_link_the_registry():
    ev, g = make_event("Ana", "Ben"), FakeGoogle()
    ev.registry_url = "https://my.babylist.com/stan-lee-baby-registry"
    tools = AgentTools(ev, g)
    tools.tool_send_email("ana@x.com", "Hi Ana! Which dates work?\n\nStan")  # the agent forgot the link
    assert g.sent[0]["body"].endswith("baby registry:\nhttps://my.babylist.com/stan-lee-baby-registry")
    tools.tool_send_email("ben@x.com", f"Hi Ben! Registry: {ev.registry_url}\n\nStan")  # it remembered
    assert g.sent[1]["body"].count(ev.registry_url) == 1
    ev.guests[0].last_reply_at = datetime.now(timezone.utc).isoformat()
    AgentTools(ev, g).tool_send_email("ana@x.com", "Thanks! Which Saturday?")  # follow-ups don't repeat it
    assert ev.registry_url not in g.sent[2]["body"]


def test_ask_new_date_needs_the_organizer():
    ev = make_event("Ana")
    with pytest.raises(ToolError, match="Only the organizer"):
        AgentTools(ev, FakeGoogle()).tool_ask_new_date("When?")
    AgentTools(ev, FakeGoogle(), organizer_spoke=True).tool_ask_new_date("When should it move to?")
    assert widgets(ev.id, "new_date")


# ---------------- inbound ----------------


def test_inbound_receive(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    ev = make_event("Ana")
    form = reply_form(ev, "Ana", "Fridays work.\n\nOn Sat, Nov 14, 2026 at 3:00 PM Stan <s@g.com> wrote:\n> hi")
    assert inbound.receive(form) == ("ev1", "queued")
    assert inbound.receive(form) == ("ev1", "duplicate")
    assert inbound.receive(reply_form(ev, "Ana", "hi", sender="evil@x.com")) == ("ev1", "sender mismatch")
    assert db.pending_inbox("ev1")[0]["payload"]["body"] == "Fridays work."
    stray = inbound.to_sendgrid_form("a@x.com", "", "someone@else.com", "hi", "hi")
    assert inbound.receive(stray) == (None, "ignored")


def test_webhook_requires_secret(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    ev = make_event("Ana")
    client = TestClient(webapp.app)
    files = {k: (None, v) for k, v in reply_form(ev, "Ana", "Fri").items()}
    assert client.post("/inbound/sendgrid", params={"key": "nope"}, files=files).status_code == 403
    assert client.post("/inbound/sendgrid", params={"key": config.INBOUND_SECRET}, files=files).json() == {"status": "queued"}


# ---------------- setup: chat before and after connecting Gmail ----------------


def answer(client, eid, kind, value):
    w = widgets(eid, kind)
    assert w, f"no open {kind} widget"
    return client.post(f"/events/{eid}/widget", json={"message_id": w[-1]["id"], "value": value})


def connect_gmail(client, monkeypatch, eid):
    """Run the Google sign-in round trip with Google faked."""
    import google_api
    monkeypatch.setattr(google_api, "start_sign_in", lambda: ("https://accounts.google.com/x", "st", "ver"))
    monkeypatch.setattr(google_api, "finish_sign_in", lambda url, state, verifier: (USER, "{}"))
    assert client.get(f"/auth/login?event={eid}", follow_redirects=False).status_code == 307
    r = client.get("/auth/callback?state=st&code=c", follow_redirects=False)
    assert r.headers["location"] == f"/events/{eid}"


def setup_event(client, monkeypatch, setting="out", date_pick=None, samples=True, host=None) -> str:
    """Click through setup as an anonymous visitor, connect Gmail, add the sample guests."""
    r = client.get("/start", follow_redirects=False)
    eid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/events/{eid}").text
    assert "Agent activity" in page and "raffle" not in page and 'data-tab="venues"' not in page
    cards = next(m["widget"]["cards"] for m in client.get(f"/events/{eid}/state").json()["chat"] if (m["widget"] or {}).get("type") == "inspiration")
    assert [c["kind"] for c in cards] == ["restaurant", "restaurant", "picnic"]
    assert all(c["url"].startswith("https://www.google.com/maps/search/") for c in cards)
    assert client.get(f"/events/{eid}/state").json()["side"] is False  # chat only during setup
    assert answer(client, eid, "host", {"name": "Sarah Kim", "email": "nope"}).status_code == 400
    assert answer(client, eid, "host", host or {"me": True}).status_code == 200
    assert answer(client, eid, "choices", setting).status_code == 200
    assert answer(client, eid, "date", date_pick or {"mode": "window", "bucket": 1}).status_code == 200
    assert answer(client, eid, "choices", "afternoon").status_code == 200
    assert widgets(eid, "gmail")
    # Not connected yet: the guest step can't be reached, and only this browser can see the draft.
    assert TestClient(webapp.app).get(f"/events/{eid}").status_code == 404
    connect_gmail(client, monkeypatch, eid)
    assert not widgets(eid, "gmail") and widgets(eid, "guests")
    if samples:
        guests = [{"name": s["name"], "email": "", "key": i == 0} for i, s in enumerate(personas.SAMPLE_GUESTS)]
        assert answer(client, eid, "guests", {"guests": guests, "demo": True}).status_code == 200
    return eid


def test_setup_flow_window_out(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    client = TestClient(webapp.app)
    eid = setup_event(client, monkeypatch, host={"name": "Sarah Kim", "email": "sarah@x.com"})
    ev = db.load_event(eid)
    assert ev.status == "collecting" and ev.user_id == USER["id"] and ev.demo
    assert ev.registry_url == "https://my.babylist.com/stan-lee-baby-registry"
    assert ev.title == "Baby shower hosted by Sarah" and ev.start_time == "13:00" and ev.time_of_day == "afternoon"
    assert client.get(f"/events/{eid}/state").json()["side"] is True
    assert (date.fromisoformat(ev.window_start) - date.today()).days == 1
    assert (date.fromisoformat(ev.window_end) - date.today()).days == 88
    # The host is invited first, as a key person (a plus-address in demo mode), then the sample guests.
    assert [g.email for g in ev.guests] == ["stan+sarah@gmail.com", "stan+ana@gmail.com", "stan+ben@gmail.com",
                                            "stan+chloe@gmail.com"]
    assert [g.key for g in ev.guests] == [True, True, False, False] and ev.guests[0].role == "host"
    assert ev.guests[0].persona == personas.HOST_PERSONA and ev.guests[1].persona == personas.PERSONA_POOL[0]
    assert widgets(eid, "venue") and [i["kind"] for i in db.pending_inbox(eid)] == ["activated"]
    # A double click on an answered widget is refused.
    used = [m for m in db.list_chat(eid) if (m["widget"] or {}).get("type") == "host"][0]
    assert client.post(f"/events/{eid}/widget", json={"message_id": used["id"], "value": "X"}).status_code == 409


def test_setup_typed_message(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    client = TestClient(webapp.app)
    eid = client.get("/start", follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    day = (date.today() + timedelta(days=40)).isoformat()
    monkeypatch.setattr(agent, "setup_reply", lambda ev, text, step: agent.SetupAnswers(
        reply="Lovely, Maria's hosting at her place!", host_is_me=None, host_name="Maria", host_email="maria@x.com",
        setting="home", specific_date=day, window=None, time_of_day=None))
    client.post(f"/events/{eid}/message", json={"text": "My sister Maria (maria@x.com) is hosting at her place on that date"})
    ev = db.load_event(eid)
    assert ev.host_name == "Maria" and ev.host_email == "maria@x.com" and ev.setting == "home"
    assert ev.date_mode == "specific" and ev.window_start == day
    texts = [m["text"] for m in db.list_chat(eid)]
    assert "Lovely, Maria's hosting at her place!" in texts and widgets(eid)[-1]["widget"]["step"] == "time"
    assert len(widgets(eid)) == 1  # the older open question was closed

    # Off topic: a reply, nothing filled in, the open question stays.
    monkeypatch.setattr(agent, "setup_reply", lambda ev, text, step: agent.SetupAnswers(
        reply="I can only help with the shower.", host_is_me=None, host_name=None, host_email=None, setting=None,
        specific_date=None, window=None, time_of_day=None))
    client.post(f"/events/{eid}/message", json={"text": "Who won the game last night?"})
    assert widgets(eid)[-1]["widget"]["step"] == "time" and db.load_event(eid).time_of_day == ""

    # Claude unreachable: a friendly fallback.
    monkeypatch.setattr(agent, "setup_reply", lambda ev, text, step: None)
    client.post(f"/events/{eid}/message", json={"text": "hello?"})
    assert "use the options above" in db.list_chat(eid)[-1]["text"]

    monkeypatch.setattr(config, "MAX_MESSAGES_PER_EVENT", 3)
    assert client.post(f"/events/{eid}/message", json={"text": "one more"}).status_code == 429


def test_reask_and_location(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    client = TestClient(webapp.app)
    eid = client.get("/start", follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    client.post(f"/events/{eid}/locate", json={"city": "Portland", "lat": 45.5, "lng": -122.6, "postal": "97201",
                                               "timezone": "America/Los_Angeles"})
    ev = db.load_event(eid)
    assert not ev.location["in_sf"] and ev.location["postal"] == "94118" and ev.location["detected"] == "Portland"
    cards = next(m["widget"]["cards"] for m in client.get(f"/events/{eid}/state").json()["chat"] if (m["widget"] or {}).get("type") == "inspiration")
    assert "Inner Richmond" in cards[0]["line1"]  # SF venues even from Portland
    answer(client, eid, "host", {"me": True})
    client.post(f"/events/{eid}/reask", json={"step": "host"})
    assert widgets(eid)[-1]["widget"]["type"] == "host" and len(widgets(eid)) == 1
    answer(client, eid, "host", {"name": "Priya", "email": "priya@x.com"})
    assert db.load_event(eid).host_name == "Priya" and widgets(eid)[-1]["widget"]["step"] == "setting"


# ---------------- full flows through the web app and worker ----------------


def scripted_agent(event, google, happenings, save, on_sent, organizer_spoke=False, shopping_data=None):
    """Stand-in for Claude: invite, read 'free: <dates>' / 'yes' replies, propose, confirm."""
    tools = AgentTools(event, google, on_sent, organizer_spoke)
    text = "\n".join(happenings)
    for g in event.attending:
        if g.round_contacted < event.round or not g.transcript:
            tools.tool_send_email(g.email, f"Hi {g.first_name}, which dates work?")
        elif g.status == "invited" and g.transcript[-1]["direction"] == "in":
            body = g.transcript[-1]["body"]
            dates = body.split("free: ")[1].split(",") if "free: " in body else [event.window_start]
            tools.tool_record_availability(g.email, dates, "")
    s = event.date_summary()
    if event.status == "collecting" and s["all_responded"] and s["dates_everyone_free"]:
        tools.tool_propose_date(s["dates_everyone_free"][0], "Works for everyone.", s["dates_everyone_free"][1:])
    if "booked" in text and event.status == "booked":
        for g in event.attending:
            tools.tool_send_email(g.email, "It's booked, see you there!")
    save()


def reply(ev: Event, g: Guest, text: str) -> None:
    form = inbound.to_sendgrid_form(g.email, g.name, inbound.reply_address(g.token), "Re", text)
    assert inbound.receive(form)[1] == "queued"


def test_window_flow_with_restaurant(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(worker, "run_agent", scripted_agent)
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    sims = []
    monkeypatch.setattr(personas, "on_sent", lambda ev, g: sims.append(g.email))
    client = TestClient(webapp.app)
    process = lambda eid: worker.process(eid, google_factory=lambda token: fake)

    eid = setup_event(client, monkeypatch)
    process(eid)  # invitations
    assert len(fake.sent) == 3 and fake.sent[0]["subject"] == "Baby shower hosted by Stan: finding a date"
    assert any("Invitations sent to Ana, Ben and Chloe" == m["text"] for m in db.list_chat(eid))

    # The organizer picks the closest restaurant from the chat.
    rid = widgets(eid, "venue")[0]["widget"]["options"][0]["id"]
    assert rid.startswith("resy-")
    assert answer(client, eid, "venue", rid).status_code == 200
    process(eid)
    ev = db.load_event(eid)
    r = ev.restaurant
    assert r and r.real_email == venues.by_id(rid)["email"] and r.email.startswith("stan+") and r.email != r.real_email
    assert fake.sent[-1]["to"].endswith(f"<{r.email}>") and fake.sent[-1]["subject"].startswith("Group booking inquiry")
    assert sims[-1] == r.email  # restaurants are always simulated

    d = ev.window_dates()
    for g, dates in zip(ev.people, [[d[5], d[9]], [d[9], d[12]], [d[9]]]):
        reply(ev, g, "free: " + ",".join(dates))
    process(eid)
    assert db.load_event(eid).status == "collecting"  # still waiting on the restaurant
    reply(ev, r, "free: " + ",".join(d[8:20]))
    process(eid)
    ev = db.load_event(eid)
    assert ev.status == "proposed" and ev.proposal["date"] == d[9]
    state = client.get(f"/events/{eid}/state").json()
    assert venues.by_id(rid)["name"] in state["panel"] and "venue-mini" in state["panel"]

    # Book from the chat: calendar invites only the guests; the restaurant gets a headcount email.
    assert answer(client, eid, "proposal", d[9]).status_code == 200
    process(eid)
    # The restaurant gets booked first: nothing is confirmed until the organizer says they've reserved.
    ev = db.load_event(eid)
    assert ev.status == "proposed" and ev.pending_book == d[9] and not fake.created
    assert widgets(eid, "venue_booked")[-1]["widget"]["day"] == d[9]
    sent_before = len(fake.sent)
    assert answer(client, eid, "venue_booked", "yes").status_code == 200
    process(eid)
    ev = db.load_event(eid)
    assert ev.status == "booked" and fake.created[0]["attendees"] == [g.email for g in ev.people]
    assert ev.venue["booked_for"] == d[9] and ev.pending_book is None and len(fake.sent) > sent_before
    assert venues.by_id(rid)["name"] in fake.created[0]["location"]
    assert ev.venue["check"]["times"] == ["13:00", "13:30"]
    assert all(g.confirmed_round == 0 for g in ev.attending)


def test_specific_date_at_home(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(worker, "run_agent", scripted_agent)
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    monkeypatch.setattr(personas, "on_sent", lambda ev, g: None)
    client = TestClient(webapp.app)
    day = (date.today() + timedelta(days=30)).isoformat()
    eid = setup_event(client, monkeypatch, setting="home", date_pick={"mode": "specific", "date": day})
    ev = db.load_event(eid)
    assert ev.status == "booked" and ev.booked_date == day and ev.window_start == ev.window_end == day
    assert widgets(eid, "shopping_offer") and not widgets(eid, "venue")
    worker.process(eid, google_factory=lambda token: fake)
    ev = db.load_event(eid)
    assert fake.created and fake.created[0]["day"] == day and ev.calendar_date == day  # on the calendar right away
    assert fake.sent[0]["subject"] == "Baby shower hosted by Stan: RSVP"
    assert "Added to your Google Calendar" in " ".join(m["text"] for m in db.list_chat(eid))


def test_specific_date_picnic(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(worker, "run_agent", scripted_agent)
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    monkeypatch.setattr(personas, "on_sent", lambda ev, g: None)
    client = TestClient(webapp.app)
    day = (date.today() + timedelta(days=30)).isoformat()
    eid = setup_event(client, monkeypatch, date_pick={"mode": "specific", "date": day})
    worker.process(eid, google_factory=lambda token: fake)
    pid = next(o["id"] for o in widgets(eid, "venue")[0]["widget"]["options"] if o["kind"] == "picnic")
    site = venues.by_id(pid)

    # Taken: the three nearest free tables are offered instead.
    others = list({s["park"]: s for s in venues.picnic_sites() if s["park"] != site["park"]}.values())[:5]
    monkeypatch.setattr(venues, "picnic_search", lambda *a, **k: {"ok": True, "seconds": 0.2, "error": "", "items":
                        [{"facility_id": site["facility_id"], "available": False}] +
                        [{"facility_id": s["facility_id"], "available": True} for s in others]})
    answer(client, eid, "venue", pid)
    worker.process(eid, google_factory=lambda token: fake)
    ev = db.load_event(eid)
    assert ev.venue["check"]["status"] == "taken" and len(ev.picnic_alternatives) == 3
    alt = widgets(eid, "venue")[-1]["widget"]["options"]
    assert [o["id"] for o in alt] == ev.picnic_alternatives and not fake.created
    assert widgets(eid, "shopping_offer")  # a picnic gets the shopping list offer

    # Pick a free one, book it on SF Rec & Park, and the date goes on the calendar.
    monkeypatch.setattr(venues, "picnic_search", lambda *a, **k: {"ok": True, "seconds": 0.2, "error": "", "items":
                        [{"facility_id": s["facility_id"], "available": True} for s in others]})
    answer(client, eid, "venue", alt[0]["id"])
    worker.process(eid, google_factory=lambda token: fake)
    assert widgets(eid, "venue_booked") and not fake.created
    answer(client, eid, "venue_booked", "yes")
    worker.process(eid, google_factory=lambda token: fake)
    ev = db.load_event(eid)
    assert ev.venue["booked"] and fake.created[0]["day"] == day


def test_key_guest_drops_out_then_new_date(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    ev = make_event("Ana", "Ben", mode="specific", demo=True)
    ev.guests[0].key = True
    tools = AgentTools(ev, fake)
    for g in ev.guests:
        tools.tool_send_email(g.email, "You're invited!")
        tools.tool_record_availability(g.email, [ev.booked_date], "")
    worker._ensure_calendar(ev, lambda: fake)
    assert fake.created and ev.calendar_date == ev.booked_date

    # Ana (key) can't come: the agent asks; approving shows the date picker.
    AgentTools(ev, fake).tool_ask_organizer("Ana can't make it. Find a new date?", "Find a new date", "Keep it", "reschedule")
    qid = ev.pending_question["id"]
    note = worker._apply_answer(ev, {"question_id": qid, "choice": "approve"})
    assert "picking the new date" in note and widgets(ev.id, "new_date") and ev.pending_question is None
    assert worker._apply_answer(ev, {"question_id": qid, "choice": "approve"}) is None  # stale click

    new_day = (date.today() + timedelta(days=21)).isoformat()
    note = worker._reschedule(ev, {"mode": "specific", "date": new_day}, lambda: fake)
    assert "moving" in note and ev.round == 1 and ev.booked_date == new_day and ev.previous_dates
    assert all(g.status == "invited" and not g.available_dates for g in ev.guests)
    tools = AgentTools(ev, fake)
    for g in ev.guests:  # "the date is moving" emails go out even though they were emailed minutes ago
        tools.tool_send_email(g.email, "The date is moving, can you make it?")
    worker._ensure_calendar(ev, lambda: fake)
    assert fake.moved == [("cal-1", new_day)] and len(fake.created) == 1  # moved, not duplicated

    # Or a new window: back to finding a date.
    worker._reschedule(ev, {"mode": "window", "bucket": 2}, lambda: fake)
    assert ev.status == "collecting" and ev.date_mode == "window" and ev.round == 2
    assert (date.fromisoformat(ev.window_start) - date.today()).days == 89


def test_answer_and_simulate_routes(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    simulated = []
    monkeypatch.setattr(personas, "simulate_cant_make_it", lambda e, g: simulated.append(g.email))
    monkeypatch.setattr(webapp, "current_user", lambda request: USER)
    ev = make_event("Ana", "Ben", mode="specific", demo=True)
    ev.guests[0].key, ev.guests[0].persona = True, "Busy"
    db.save_event(ev)
    client = TestClient(webapp.app)
    assert "Ana can't make it (key)" in client.get(f"/events/{ev.id}/state").json()["top"]
    client.post(f"/events/{ev.id}/simulate-cant-make-it", json={"guest": ev.guests[0].token})
    assert simulated == ["ana@x.com"]

    AgentTools(ev, FakeGoogle()).tool_ask_organizer("Ana can't make it. Find a new date?", "Find a new date", "Keep it", "reschedule")
    db.save_event(ev)
    assert answer(client, ev.id, "question", "decline").status_code == 200
    assert [i["kind"] for i in db.pending_inbox(ev.id)] == ["demo_log", "answer"]
    assert db.list_chat(ev.id)[-1]["text"] == "Keep it"


def test_event_is_done_after_its_date(monkeypatch):
    fake = FakeGoogle()
    ev = make_event("Ana", mode="specific")
    ev.booked_date = (date.today() - timedelta(days=2)).isoformat()
    db.save_event(ev)
    runs = []
    monkeypatch.setattr(worker, "run_agent", lambda *a, **k: runs.append(1))
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    inbound.receive(reply_form(ev, "Ana", "That was fun!"))
    worker.process(ev.id, google_factory=lambda t: fake)
    ev = db.load_event(ev.id)
    assert ev.status == "done" and runs == [] and ev.guests[0].transcript[-1]["body"] == "That was fun!"


def test_reset_demo(monkeypatch):
    fake = FakeGoogle()
    ev = make_event("Ana", mode="specific")
    ev.calendar_event_id = "cal-1"
    db.save_event(ev)
    revoked = []
    monkeypatch.setattr(webapp.google_api, "GoogleAccount", lambda token: fake)
    monkeypatch.setattr(webapp.google_api, "revoke", lambda token: revoked.append(token))
    client = TestClient(webapp.app)
    monkeypatch.setattr(webapp, "current_user", lambda request: USER)
    r = client.post("/reset", follow_redirects=False)
    assert r.headers["location"] == "/?notice=reset"
    assert fake.deleted == ["cal-1"] and revoked == ["{}"]
    assert db.load_event(ev.id) is None and db.get_user(USER["id"]) is None
    assert db.event_id_for_token(ev.guests[0].token) is None
    db.save_event(ev)  # a worker finishing a run after the reset must not bring the event back
    assert db.load_event(ev.id) is None


# ---------------- the agent loop, against a mocked Anthropic API ----------------


def test_agent_loop(monkeypatch):
    ev, fake = make_event("Ana"), FakeGoogle()
    calls = []
    turns = [
        {"stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "Ana hasn't been invited yet; I'll write to her.", "signature": "s"},
            {"type": "tool_use", "id": "tu1", "name": "send_email",
             "input": {"to": "ana@x.com", "body": "Hi Ana! Which dates work?\n\nStan"}},
            {"type": "tool_use", "id": "tu2", "name": "message_organizer",
             "input": {"text": "Invitations are out!"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Invited Ana; waiting for her reply."}]},
    ]

    def handler(req):
        calls.append(json.loads(req.content))
        t = turns[len(calls) - 1]
        return httpx2.Response(200, json={"id": f"msg{len(calls)}", "type": "message", "role": "assistant",
                                          "model": "claude-opus-5-5", "stop_sequence": None,
                                          "usage": {"input_tokens": 1, "output_tokens": 1}, **t})

    monkeypatch.setattr(agent, "client", anthropic.Anthropic(
        api_key="test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))))
    agent.run_agent(ev, fake, ["Needs an invitation: Ana <ana@x.com>"])

    assert fake.sent[0]["body"].startswith("Hi Ana")
    assert [a["kind"] for a in ev.activity] == ["thinking", "email_out", "note"]
    assert "Invitations are out!" in [m["text"] for m in db.list_chat(ev.id)]
    req = calls[0]
    assert req["model"] == "claude-opus-5-5" and req["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert req["output_config"] == {"effort": "low"} and req["fallbacks"] == "default"
    assert all(t["strict"] for t in req["tools"])
    assert calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


# ---------------- venues ----------------


def test_nearest_venues_and_booking_window():
    near = venues.nearest({"in_sf": True, "lat": 37.7816, "lng": -122.4628})
    assert len(near["restaurants"]) == 3 and len(near["picnic"]) == 3
    assert len({p["park"] for p in near["picnic"]}) == 3  # three different parks
    assert all(r["email"] and r["photo"] for r in near["restaurants"])
    assert near["restaurants"][0]["miles"] <= near["restaurants"][-1]["miles"]
    far = (date.today() + timedelta(days=120)).isoformat()
    assert venues.booking_note(far).startswith("Booking opens on")
    assert venues.check_picnic(venues.picnic_sites()[0], far, "afternoon", {}, 10)["status"] == "not_bookable_yet"
    assert venues.picnic_block("evening") == ("13:00:00", "17:00:00")


def test_restaurant_simulator_availability():
    ev = make_event("Ana", days=40)
    open_dates, booked = personas.restaurant_availability(ev)
    sats = [d for d in ev.window_dates() if date.fromisoformat(d).weekday() == 5]
    assert booked == [sats[1], sats[3]] and not set(booked) & set(open_dates)
    specific = make_event("Ana", mode="specific", eid="ev2")
    assert personas.restaurant_availability(specific) == ([specific.window_start], [])
    s = personas._snapshot(specific, specific.guests[0])
    assert "you can make it" in s["situation"]  # RSVP guests say yes


# ---------------- shopping list ----------------


def test_shopping_build_and_edits(monkeypatch):
    planned = shopping.PlannedList(title="Baby shower picnic for 4", summary="Easy picnic food.", items=[
        shopping.PlannedItem(search_term="lemonade", display_name="Lemonade", category="Drinks", quantity=2, reason="r"),
        shopping.PlannedItem(search_term="balloons", display_name="Pink balloons", category="Decorations", quantity=1, reason="r"),
    ])
    product = lambda name, price, delivery=True: {"upc": name, "description": name, "brand": "", "size": "1 ct",
                                                  "price": price, "promo_price": None, "image": "https://img/x.jpg",
                                                  "stock": None, "url": "https://www.kroger.com/p/x", "pickup": True,
                                                  "delivery": delivery}
    monkeypatch.setattr(shopping, "plan", lambda *a: planned)
    monkeypatch.setattr(shopping, "nearest_store", lambda z: {"location_id": "1", "name": "Foodsco - Folsom", "zip": z})
    monkeypatch.setattr(shopping, "search_products", lambda term, loc, limit=8: {
        "lemonade": [product("Simply Lemonade", 3.49), product("Minute Maid Lemonade", 2.99)],
        "balloons": [product("Number 4 Balloon", 12.99, delivery=False)],
        "pink balloons": [product("Pink Latex Balloons", 4.99, delivery=False)]}.get(term, []))
    monkeypatch.setattr(shopping, "pick_matches", lambda items: {
        0: shopping.MatchChoice(item_number=0, best_candidate=1, better_search_term=""),
        1: shopping.MatchChoice(item_number=1, best_candidate=-1, better_search_term="pink balloons")})
    logs = []
    db.update_shopping("ev1", lambda _: {"status": "planning", "items": []})
    shopping._build("ev1", "picnic", 4, "", "94118", logs.append)
    data = db.get_shopping("ev1")
    assert data["status"] == "ready" and data["store"]["name"] == "Foodsco - Folsom"
    assert data["items"][0]["matches"][0]["description"] == "Minute Maid Lemonade"  # Claude's pick first
    assert data["items"][1]["matches"][0]["description"] == "Pink Latex Balloons"  # the retry search
    assert shopping.totals(data) == {"total": round(2 * 2.99 + 4.99, 2), "unavailable": 1}  # balloons: no delivery
    shopping.edit("ev1", "swap", 0, 1)
    shopping.edit("ev1", "qty", 0, 30)
    shopping.edit("ev1", "remove", 1)
    data = db.get_shopping("ev1")
    assert data["items"][0]["chosen"] == 1 and data["items"][0]["quantity"] == 24 and data["items"][1]["removed"]
    assert shopping.totals(data) == {"total": round(24 * 3.49, 2), "unavailable": 0}
    assert any("found 2 of 2" in t for t in logs)


def test_shopping_offer_starts_the_build(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    started = []
    monkeypatch.setattr(shopping, "start", lambda *a, **k: started.append(a))
    client = TestClient(webapp.app)
    eid = setup_event(client, monkeypatch, setting="home")
    assert answer(client, eid, "shopping_offer", "yes").status_code == 200
    assert started and started[0][1] == "home" and started[0][2] == 4  # three guests + the organizer


# ---------------- small pieces ----------------


def test_sign_in_url(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")
    import google_api
    url, state, verifier = google_api.start_sign_in()
    assert "gmail.send" in url and "code_challenge=" in url and "access_type=offline" in url
    assert "prompt=select_account+consent" in url  # the account picker and "Allow" screen every time
    assert "gmail.readonly" not in url and "gmail.modify" not in url and verifier


def test_plus_addresses_are_unique():
    taken = set()
    for name in ["Ana Ruiz", "Ana Lee", "", "Ana"]:
        taken.add(personas.plus_address("stan+x@gmail.com", name, taken))
    assert taken == {"stan+ana@gmail.com", "stan+ana2@gmail.com", "stan+guest@gmail.com", "stan+ana3@gmail.com"}


class FakePeople(FakeGoogle):
    warmed = False

    def warm_up_contacts(self):
        FakePeople.warmed = True

    def suggested_contacts(self):
        return [{"name": "Sarah Kim", "email": "sarah@x.com"}]

    def search_contacts(self, q):
        return [{"name": "Sam Ortiz", "email": "sam@x.com"}] if q.lower().startswith("sa") else []


def test_contacts_endpoints(monkeypatch):
    monkeypatch.setattr(webapp, "current_user", lambda request: USER)
    monkeypatch.setattr(webapp.google_api, "GoogleAccount", FakePeople)
    client = TestClient(webapp.app)
    d = client.post("/api/contacts/warmup").json()
    assert d == {"available": True, "suggested": [{"name": "Sarah Kim", "email": "sarah@x.com"}]} and FakePeople.warmed
    assert client.get("/api/contacts/search", params={"q": "sa"}).json()["results"][0]["email"] == "sam@x.com"

    def broken(token):
        raise RuntimeError("People API not enabled")
    monkeypatch.setattr(webapp.google_api, "GoogleAccount", broken)
    assert client.post("/api/contacts/warmup").json() == {"available": False, "reason": "no_account"}

    class Disabled(FakePeople):
        def warm_up_contacts(self):
            raise RuntimeError("People API has not been used in project 123 before or it is disabled.")
    monkeypatch.setattr(webapp.google_api, "GoogleAccount", Disabled)
    assert client.post("/api/contacts/warmup").json() == {"available": False, "reason": "api_disabled"}


def test_people_to_guests():
    import google_api
    people = [
        {"names": [{"displayName": "Sarah Kim"}], "emailAddresses": [{"value": "Sarah@X.com"}, {"value": "s2@x.com"}]},
        {"names": [{"displayName": "Dup"}], "emailAddresses": [{"value": "sarah@x.com"}]},
        {"names": [{"displayName": "No Email"}]},
        {"emailAddresses": [{"value": "anon@x.com"}]},
    ]
    assert google_api._people_to_guests(people) == [
        {"name": "Sarah Kim", "email": "sarah@x.com"}, {"name": "", "email": "anon@x.com"}]


def test_email_cards_merge():
    chat.email_card("ev9", "Invitations sent to {names}", merge_key="k", names=["Ana"])
    chat.email_card("ev9", "Invitations sent to {names}", merge_key="k", names=["Ben"])
    chat.email_card("ev9", "Invitations sent to {names}", merge_key="k", names=["Chloe"])
    assert [m["text"] for m in db.list_chat("ev9")] == ["Invitations sent to Ana, Ben and Chloe"]


def test_email_stack_and_invitee_status():
    ev, fake = make_event("Ana", "Ben", "Cy"), FakeGoogle()
    tools = AgentTools(ev, fake)
    for g in ev.guests:
        tools.tool_send_email(g.email, f"Hi {g.name},\n\nSarah's shower is coming up! Which dates work?\n\nStan")
    card = db.list_chat(ev.id)[-1]
    assert card["text"] == "Invitations sent to Ana, Ben and Cy"
    assert [i["token"] for i in card["widget"]["items"]] == [g.token for g in ev.guests]
    assert card["widget"]["items"][0]["preview"] == "Sarah's shower is coming up! Which dates work?"
    d = ev.window_dates()
    ana, ben, cy = ev.guests
    tools.tool_record_availability(ana.email, d[:3], "")
    tools.tool_mark_declined(cy.email, "")
    assert chat.person_status(ev, ana) == ("yellow", "Replied: free on 3 days")
    assert chat.person_status(ev, ben) == ("yellow", "Waiting for a reply")
    assert chat.person_status(ev, cy) == ("red", "Can't make it")
    ev.status, ev.booked_date = "booked", d[1]
    assert chat.person_status(ev, ana) == ("green", "Confirmed")
    ev.booked_date = d[5]
    assert chat.person_status(ev, ana) == ("red", "Can't make that date")


def test_venue_reminders(monkeypatch):
    ev = make_event("Ana", demo=True)
    ev.setting = "out"
    assert not worker.venue_nudge_due(ev)  # no activity yet
    ev.log("action", "Sent invitations")
    assert not worker.venue_nudge_due(ev)  # just now: not quiet yet
    ev.activity[-1]["at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    assert any(d.startswith("Venue reminder due") for d in worker.due_items(ev))
    runs = []
    monkeypatch.setattr(worker, "run_agent", lambda e, g, happenings, **k: runs.append(happenings))
    db.save_event(ev)
    worker.process(ev.id, google_factory=lambda t: FakeGoogle())
    ev = db.load_event(ev.id)
    assert ev.venue_nudges == 1 and any("Venue reminder due" in h for h in runs[0])
    assert not worker.venue_nudge_due(ev)  # the next one waits longer
    AgentTools(ev, FakeGoogle()).tool_offer_venues("Want to pick a venue so I can check the date?")
    assert widgets(ev.id, "venue")
    ev.venue = {"type": "picnic", "id": "x", "name": "Park"}
    with pytest.raises(ToolError, match="already picked"):
        AgentTools(ev, FakeGoogle()).tool_offer_venues("again")
    assert not worker.needs_venue(ev)


def test_make_list_from_the_shopping_tab(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    started = []
    monkeypatch.setattr(shopping, "start", lambda *a, **k: started.append(a) or
                        db.update_shopping(a[0], lambda _: {"status": "planning", "items": []}))
    client = TestClient(webapp.app)
    eid = setup_event(client, monkeypatch, setting="home")
    panel = client.get(f"/events/{eid}/state").json()["panel"]
    assert 'data-attn="1"' in panel and "data-make-list" in panel  # red dot + button in the tab
    assert client.post(f"/events/{eid}/make-list").status_code == 200
    assert started and not widgets(eid, "shopping_offer")  # the chat's offer is answered too
    assert db.list_chat(eid)[-1]["text"] == "Yes, make the list"
    assert client.post(f"/events/{eid}/make-list").status_code == 409  # only once
    assert 'data-attn="1"' not in client.get(f"/events/{eid}/state").json()["panel"]



def ready_list(eid="ev1"):
    product = lambda name, price: {"upc": name, "description": name, "brand": "", "size": "", "price": price,
                                   "promo_price": None, "image": None, "stock": None, "url": None, "pickup": True,
                                   "delivery": True}
    db.update_shopping(eid, lambda _: {"status": "ready", "store": {"location_id": "1", "name": "Foodsco - Folsom"},
                                       "items": [{"search_term": "lemonade", "display_name": "Lemonade", "category": "Drinks",
                                                  "quantity": 2, "reason": "", "matches": [product("Simply Lemonade", 3.49)],
                                                  "chosen": 0, "removed": False}], "delivery": None})
    return product


def test_agent_edits_the_shopping_list(monkeypatch):
    ev = make_event("Ana")
    product = ready_list()
    monkeypatch.setattr(shopping, "search_products", lambda term, loc, limit=8: [product("TruMoo Chocolate Milk", 2.99)])
    tools = AgentTools(ev, FakeGoogle())
    out = tools.tool_add_to_shopping_list([{"name": "Chocolate milk", "search_term": "chocolate milk", "quantity": 2}])
    assert "TruMoo Chocolate Milk, $2.99" in out
    tools.tool_change_shopping_quantity("lemonade", 4)
    tools.tool_remove_from_shopping_list(["chocolate"])
    items = db.get_shopping("ev1")["items"]
    assert items[0]["quantity"] == 4 and items[1]["display_name"] == "Chocolate milk" and items[1]["removed"]
    with pytest.raises(ToolError, match="isn't on the list"):
        tools.tool_change_shopping_quantity("cake", 2)
    assert shopping.edit("ev1", "add", value="napkins")["items"][-1]["display_name"] == "Napkins"  # the tab's add box


def test_grocery_delivery_flow(monkeypatch):
    monkeypatch.setattr(worker, "run_agent", lambda *a, **k: None)
    fake = FakeGoogle()
    ev = make_event("Ana", mode="specific", demo=True)  # booked on a fixed date
    ready_list()
    assert shopping.delivery_slots("13:00") == ["08:00-10:00", "09:00-11:00", "10:00-12:00"]
    assert shopping.slot_label("10:00-12:00") == "10am–12pm"
    shopping.edit("ev1", "schedule", value="10:00-12:00")
    assert worker.delivery_due(ev)
    worker.process("ev1", google_factory=lambda t: fake)
    d = db.get_shopping("ev1")["delivery"]
    reminder = fake.created[-1]
    assert reminder["title"] == "Grocery delivery for the baby shower" and reminder["day"] == ev.booked_date
    assert reminder["start_time"] == "10:00" and reminder["attendees"] == [] and d["reminder_id"] == "cal-1"
    assert d["status"] == "scheduled" and not widgets("ev1", "payment")  # the shower is a week out: order later

    # Order time arrives (the demo button does this): the agent asks for payment.
    db.update_shopping("ev1", lambda x: {**x, "delivery": {**x["delivery"], "order_at": datetime.now(timezone.utc).isoformat()}})
    worker.process("ev1", google_factory=lambda t: fake)
    assert db.get_shopping("ev1")["delivery"]["status"] == "awaiting_payment" and widgets("ev1", "payment")
    db.add_inbox("ev1", "payment", {"ok": True})
    worker.process("ev1", google_factory=lambda t: fake)
    assert db.get_shopping("ev1")["delivery"]["status"] == "ordered"
    assert "no card was charged" in db.list_chat("ev1")[-1]["text"]

    # Cancelling removes the calendar reminder.
    shopping.edit("ev1", "cancel_delivery")
    worker.process("ev1", google_factory=lambda t: fake)
    assert fake.deleted == ["cal-1"] and not db.get_shopping("ev1")["delivery"]["reminder_id"]


def test_demo_stall_after_vague_reply(monkeypatch):
    ev = make_event("Ben", demo=True)
    tools = AgentTools(ev, FakeGoogle())
    tools.tool_send_email("ben@x.com", "Which dates work?")
    g = ev.guests[0]
    old = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    g.transcript.append({"direction": "in", "at": old, "body": "Weekends are usually best, I'll get back to you."})
    g.last_reply_at = old
    assert any(d.startswith("Unanswered reply") for d in worker.due_items(ev))
    runs = []
    monkeypatch.setattr(worker, "run_agent", lambda e, gg, happenings, **k: runs.append(happenings))
    db.save_event(ev)
    worker.process(ev.id, google_factory=lambda t: FakeGoogle())
    assert any("Unanswered reply" in h for h in runs[0])
    assert not any(d.startswith("Unanswered reply") for d in worker.due_items(db.load_event(ev.id)))  # only once


def test_view_all_venues(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    monkeypatch.setattr(venues, "resy_open_days", lambda rid, party, days=30: {"ok": True, "open": 12, "days": 30, "next": None})
    client = TestClient(webapp.app)
    eid = client.get("/start", follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    html = client.get(f"/events/{eid}/venues?kind=restaurants").text
    assert html.count('class="vcard-all"') == len(venues.restaurants())
    assert "Tables for 10 on 12 of the next 30 days" in html and "mailto:" in html and "tel:" in html
    html = client.get(f"/events/{eid}/venues?kind=picnic").text
    assert html.count('class="vcard-all"') == len(venues.picnic_sites()) and "Book on SF Rec &amp; Park" in html
    assert "pick a date to check availability" in html


def test_choose_venue_from_the_panel(monkeypatch):
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    monkeypatch.setattr(venues, "resy_open_days", lambda rid, party, days=30: {"ok": True, "open": 3, "days": 30, "next": None})
    client = TestClient(webapp.app)
    eid = client.get("/start", follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    pid = venues.picnic_sites()[-1]["id"]  # one that isn't among the chat's cards
    assert client.post(f"/events/{eid}/choose-venue", json={"venue_id": pid}).status_code == 400  # still setting up
    assert "data-choose" not in client.get(f"/events/{eid}/venues?kind=picnic").text
    eid = setup_event(client, monkeypatch)  # going out; the chat now asks which venue
    assert widgets(eid, "venue") and "Choose this venue" in client.get(f"/events/{eid}/venues?kind=picnic").text
    assert client.post(f"/events/{eid}/choose-venue", json={"venue_id": pid}).status_code == 200
    assert not widgets(eid, "venue")  # the chat's question is answered
    assert db.list_chat(eid)[-1]["text"].startswith("Let's go with")
    assert db.pending_inbox(eid)[-1] == {**db.pending_inbox(eid)[-1], "kind": "venue", "payload": {"venue_id": pid}}


def test_google_step_always_shows(monkeypatch):
    """Even when this browser is already signed in, a new shower goes through Connect Google."""
    monkeypatch.setattr(worker, "wake", lambda _id: None)
    client = TestClient(webapp.app)
    first = setup_event(client, monkeypatch)  # signs in along the way
    eid = client.get("/events/new", follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    assert eid != first and db.load_event(eid).user_id == USER["id"]
    for kind, value in [("host", {"me": True}), ("choices", "home"), ("date", {"mode": "window", "bucket": 1}),
                        ("choices", "morning")]:
        assert answer(client, eid, kind, value).status_code == 200
    assert widgets(eid, "gmail") and not widgets(eid, "guests")
    connect_gmail(client, monkeypatch, eid)
    assert not widgets(eid, "gmail") and widgets(eid, "guests")



def test_picnic_dates_checked_before_proposing(monkeypatch):
    ev = make_event("Ana", "Ben", days=14)
    site = venues.picnic_sites()[0]
    ev.setting, ev.venue = "out", {"type": "picnic", "id": site["id"], "name": site["name"], "url": site["book_url"]}
    d = ev.window_dates()
    tools = AgentTools(ev, FakeGoogle())
    for g in ev.guests:
        tools.tool_record_availability(g.email, d[3:6], "")
    with pytest.raises(ToolError, match="Check the picnic area first"):
        tools.tool_propose_date(d[3], "x", [])
    taken = {d[3]}
    monkeypatch.setattr(venues, "picnic_search", lambda lat, lng, day, *a, **k: {"ok": True, "seconds": 0.1, "error": "",
                        "items": [] if day in taken else [{"facility_id": site["facility_id"], "available": True}]})
    out = json.loads(tools.tool_check_venue_availability(d[3:6]))
    assert out["availability"] == {d[3]: "taken", d[4]: "free", d[5]: "free"}
    with pytest.raises(ToolError, match="taken"):
        tools.tool_propose_date(d[3], "x", [])
    tools.tool_propose_date(d[4], "Free for everyone and the park.", [d[3], d[5]])
    assert ev.proposal["alternatives"] == [d[5]]  # the taken date is dropped
