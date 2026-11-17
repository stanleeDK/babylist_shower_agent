"""Venues: SF restaurants that take groups of 10 (from Resy) and SF picnic areas.

The lists are saved snapshots (data/*.json, built offline by scripts/build_*.py), so they
show instantly. Live checks happen only for the venue the organizer picks:
- Resy: open times for the party on the date (one call to Resy's unofficial web API).
- SF Rec & Park: whether a picnic area is free on the date (one search on the city's
  reservation system), and the nearest free ones if it isn't.
Both are best effort: if a check fails, the flow carries on without it.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import httpx

import config

DATA = Path(__file__).parent / "data"
PICNIC_BOOKING_DAYS = (3, 90)  # SF Rec & Park takes online picnic bookings 3-90 days ahead


@lru_cache(maxsize=1)
def restaurants() -> list[dict]:
    path = DATA / "restaurants.json"
    return json.loads(path.read_text()) if path.exists() else []


@lru_cache(maxsize=1)
def picnic_sites() -> list[dict]:
    path = DATA / "picnic_sites.json"
    return json.loads(path.read_text()) if path.exists() else []


def by_id(venue_id: str) -> dict | None:
    return next((v for v in restaurants() + picnic_sites() if v["id"] == venue_id), None)


def _miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def search_point(location: dict) -> tuple[float, float]:
    """Where to search from: the visitor's location if they're in SF, otherwise central SF."""
    if location.get("in_sf") and location.get("lat") is not None:
        return location["lat"], location["lng"]
    return config.SF_CENTER


def nearest(location: dict, n: int = 3) -> dict:
    """{"restaurants": [...], "picnic": [...]}, each with "miles". Picnic areas come from
    different parks, so the three options aren't all in one park."""
    lat, lng = search_point(location)
    with_miles = lambda v: {**v, "miles": round(_miles(lat, lng, v["lat"], v["lng"]), 1)}
    rest = sorted((with_miles(v) for v in restaurants()), key=lambda v: v["miles"])[:n]
    parks, picnic = set(), []
    for v in sorted((with_miles(v) for v in picnic_sites()), key=lambda v: (v["miles"], -(v["capacity"] or 0))):
        if v["park"] not in parks:
            parks.add(v["park"])
            picnic.append(v)
        if len(picnic) == n:
            break
    return {"restaurants": rest, "picnic": picnic}


def in_sf(lat: float | None, lng: float | None) -> bool:
    return lat is not None and lng is not None and 37.70 <= lat <= 37.84 and -122.52 <= lng <= -122.35


# ---------------- Resy (unofficial web API, public key from resy.com) ----------------

RESY_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
RESY_HEADERS = {
    "Authorization": f'ResyAPI api_key="{RESY_KEY}"',
    "Accept": "application/json",
    "Origin": "https://resy.com",
    "Referer": "https://resy.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def resy_times(resy_id: int, day: str, party_size: int, around: str | None) -> dict:
    """Open reservation times on Resy for the party on that day, closest to `around` first.
    Returns {"ok", "times", "seconds", "error"}."""
    started = time.monotonic()
    try:
        resp = httpx.get(
            "https://api.resy.com/4/find",
            params={"lat": 0, "long": 0, "day": day, "party_size": party_size, "venue_id": resy_id},
            headers=RESY_HEADERS,
            timeout=6,
        )
        resp.raise_for_status()
        times = []
        for venue in (resp.json().get("results") or {}).get("venues") or []:
            for slot in venue.get("slots") or []:
                start = ((slot.get("date") or {}).get("start") or "")
                if " " in start and start.split(" ", 1)[1][:5] not in times:
                    times.append(start.split(" ", 1)[1][:5])
        if around:
            target = int(around[:2]) * 60 + int(around[3:5])
            times.sort(key=lambda t: abs(int(t[:2]) * 60 + int(t[3:5]) - target))
        return {"ok": True, "times": times[:4], "seconds": round(time.monotonic() - started, 1), "error": ""}
    except Exception as e:
        return {"ok": False, "times": [], "seconds": round(time.monotonic() - started, 1), "error": str(e)[:200]}


# ---------------- SF Rec & Park (ActiveNet, the city's reservation system) ----------------

ACTIVENET = "https://anc.apm.activecommunities.com/sfrecpark"
PICNIC_TYPES = [101, 102]  # Picnic Area - Half Day / Full Day


def picnic_block(time_of_day: str) -> tuple[str, str]:
    """Half-day blocks. Evening falls back to the afternoon: parks close at dusk."""
    return ("09:00:00", "13:00:00") if time_of_day == "morning" else ("13:00:00", "17:00:00")


def booking_note(day: str, today: date | None = None) -> str:
    """Empty if the date can be booked online now; otherwise when it can be."""
    today = today or date.today()
    ahead = (date.fromisoformat(day) - today).days
    if ahead > PICNIC_BOOKING_DAYS[1]:
        opens = date.fromisoformat(day) - timedelta(days=PICNIC_BOOKING_DAYS[1])
        return f"Booking opens on {opens:%a %b} {opens.day} (90 days ahead)."
    if ahead < PICNIC_BOOKING_DAYS[0]:
        return "SF Rec & Park needs picnic bookings at least 3 days ahead, so it may not be bookable online."
    return ""


def picnic_search(lat: float, lng: float, day: str, time_of_day: str, people: int = 10) -> dict:
    """One availability search, nearest first. Returns {"ok", "items", "seconds", "error"}
    where items are {"facility_id", "available"}."""
    started = time.monotonic()
    start, end = picnic_block(time_of_day)
    body = {
        "name": "", "attendee": max(people, 10),
        "date_times": [{"from_date_time": f"{day} {start}", "to_date_time": f"{day} {end}"}],
        "event_type_ids": [], "facility_type_ids": PICNIC_TYPES, "reservation_group_ids": [],
        "amenity_ids": [], "facility_id": 0, "equipment_id": 0, "center_id": 0, "resource_type": 0,
        "client_coordinate": f"{lat:.6f},{lng:.6f}", "order_by_field": "distance", "order_direction": "asc",
        "page_size": 20, "start_index": 0, "search_client_id": "", "date_time_length": None,
        "full_day_booking": False, "center_ids": [], "specify_start_and_end_times": False,
    }
    try:
        with httpx.Client(timeout=8, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; baby-shower-agent/0.2)"}) as http:
            http.get(f"{ACTIVENET}/reservation/landing/search", headers={"Accept": "text/html"})  # session cookie
            resp = http.post(f"{ACTIVENET}/rest/reservation/resource", params={"locale": "en-US"}, json=body,
                             headers={"Accept": "application/json", "Content-Type": "application/json;charset=utf-8"})
        data = resp.json()
        if (data.get("headers") or {}).get("response_code") != "0000":
            raise RuntimeError((data.get("headers") or {}).get("response_message") or f"HTTP {resp.status_code}")
        items = [{"facility_id": i["id"], "available": (i.get("availability") or "") == "Available"}
                 for i in (data.get("body") or {}).get("items") or []]
        return {"ok": True, "items": items, "seconds": round(time.monotonic() - started, 1), "error": ""}
    except Exception as e:
        return {"ok": False, "items": [], "seconds": round(time.monotonic() - started, 1), "error": str(e)[:200]}


def check_picnic(site: dict, day: str, time_of_day: str, location: dict, people: int) -> dict:
    """Is this picnic area free that day, and if not, which of our saved sites nearby are?
    Returns {"status": "free"|"taken"|"not_bookable_yet"|"unknown", "note", "alternatives": [ids],
    "seconds", "error"}."""
    note = booking_note(day)
    if note.startswith("Booking opens"):
        return {"status": "not_bookable_yet", "note": note, "alternatives": [], "seconds": 0, "error": ""}
    found = picnic_search(site["lat"], site["lng"], day, time_of_day, people)
    if not found["ok"]:
        return {"status": "unknown", "note": note, "alternatives": [], "seconds": found["seconds"], "error": found["error"]}
    # The city's search only lists picnic areas that are free for that date and time, nearest
    # first. We search from the site itself, so if it isn't listed, it's taken.
    status = {i["facility_id"]: i["available"] for i in found["items"]}
    free = bool(status.get(site["facility_id"]))
    lat, lng = search_point(location)
    if not free:  # find free ones near the organizer, too
        again = picnic_search(lat, lng, day, time_of_day, people)
        status.update({i["facility_id"]: i["available"] for i in again["items"]})
    alternatives = []
    if not free:
        lat, lng = search_point(location)
        known = {s["facility_id"]: s for s in picnic_sites()}
        free_known = [known[f] for f, ok in status.items() if ok and f in known and f != site["facility_id"]]
        free_known.sort(key=lambda s: _miles(lat, lng, s["lat"], s["lng"]))
        parks: set[str] = set()
        for s in free_known:
            if s["park"] not in parks:
                parks.add(s["park"])
                alternatives.append(s["id"])
            if len(alternatives) == 3:
                break
    return {
        "status": "free" if free else "taken",
        "note": note,
        "alternatives": alternatives,
        "seconds": found["seconds"],
        "error": "",
    }


# ---------------- browsing every venue (the "View all" panel) ----------------

_avail_cache: dict[tuple, tuple[float, dict]] = {}
AVAIL_TTL = 600  # seconds; availability changes, but not by the minute


def _cached(key: tuple, fetch):
    hit = _avail_cache.get(key)
    if hit and time.monotonic() - hit[0] < AVAIL_TTL:
        return hit[1]
    value = fetch()
    if value.get("ok"):
        _avail_cache[key] = (time.monotonic(), value)
    return value


def resy_open_days(resy_id: int, party_size: int, days: int = 30) -> dict:
    """How many of the next `days` days Resy shows a table for the party.
    Returns {"ok", "open", "days", "next": first open date or None}."""
    def fetch():
        start = date.today() + timedelta(days=1)
        try:
            resp = httpx.get("https://api.resy.com/4/venue/calendar", headers=RESY_HEADERS, timeout=6, params={
                "venue_id": resy_id, "num_seats": party_size,
                "start_date": start.isoformat(), "end_date": (start + timedelta(days=days - 1)).isoformat()})
            resp.raise_for_status()
            open_days = [d["date"] for d in resp.json().get("scheduled") or []
                         if ((d.get("inventory") or {}).get("reservation")) == "available"]
            return {"ok": True, "open": len(open_days), "days": days, "next": open_days[0] if open_days else None}
        except Exception as e:
            return {"ok": False, "open": 0, "days": days, "next": None, "error": str(e)[:200]}
    return _cached(("resy-days", resy_id, party_size, date.today().isoformat()), fetch)


def all_venues(kind: str, location: dict, day: str | None, time_of_day: str, party: int,
               around: str | None) -> list[dict]:
    """Every saved restaurant or picnic area, closest first, with availability: for the
    shower's date if it's set, otherwise the general picture."""
    from concurrent.futures import ThreadPoolExecutor

    lat, lng = search_point(location)
    party = max(party, 2)
    if kind == "restaurants":
        items = sorted(({**v, "miles": round(_miles(lat, lng, v["lat"], v["lng"]), 1)} for v in restaurants()),
                       key=lambda v: v["miles"])
        def check(v):
            if day:
                r = _cached(("resy-day", v["resy_id"], day, party, around), lambda: resy_times(v["resy_id"], day, party, around))
                if not r["ok"]:
                    return {"tone": "muted", "text": "Couldn't check Resy just now"}
                if r["times"]:
                    return {"tone": "green", "text": f"Open for {party} on {day}: {', '.join(r['times'])}"}
                return {"tone": "amber", "text": f"No online tables for {party} on {day}; email them about groups"}
            r = resy_open_days(v["resy_id"], party)
            if not r["ok"]:
                return {"tone": "muted", "text": "Couldn't check Resy just now"}
            if r["open"]:
                return {"tone": "green", "text": f"Tables for {party} on {r['open']} of the next {r['days']} days",
                        "next": r["next"]}
            return {"tone": "amber", "text": f"No online tables for {party} in the next {r['days']} days; email them"}
        with ThreadPoolExecutor(8) as pool:
            for v, a in zip(items, pool.map(check, items)):
                v["availability"] = a
        return items

    items = sorted(({**v, "miles": round(_miles(lat, lng, v["lat"], v["lng"]), 1)} for v in picnic_sites()),
                   key=lambda v: (v["miles"], v["park"]))
    status: dict[int, bool] = {}
    note = booking_note(day) if day else ""
    if day and not note.startswith("Booking opens"):
        points = {(round(lat, 3), round(lng, 3)), (round(config.SF_CENTER[0], 3), round(config.SF_CENTER[1], 3))}
        with ThreadPoolExecutor(2) as pool:
            for found in pool.map(lambda p: _cached(("picnic", p, day, time_of_day, party),
                                                    lambda: picnic_search(p[0], p[1], day, time_of_day, party)), points):
                status.update({i["facility_id"]: i["available"] for i in found["items"]})
    for v in items:
        free = status.get(v["facility_id"])
        if not day:
            v["availability"] = {"tone": "muted", "text": "Bookable 3 to 90 days ahead on SF Rec & Park"}
        elif note.startswith("Booking opens"):
            v["availability"] = {"tone": "muted", "text": note}
        elif free is True:
            v["availability"] = {"tone": "green", "text": f"Free on {day}"}
        elif free is False:
            v["availability"] = {"tone": "red", "text": f"Taken on {day}"}
        else:
            v["availability"] = {"tone": "muted", "text": "Check availability on SF Rec & Park"}
    return items
