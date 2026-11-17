"""Build data/restaurants.json: SF restaurants on Resy that take groups of 10 and have an email.

Run offline (it can take a few minutes; nobody is waiting):

    uv run python scripts/build_restaurants.py [--live] [--claude]

It reuses the Resy finder from ../sfparkandrec (its parsers, website email scraper and
optional Claude web search) and its on-disk cache, so a cached run makes no network calls.
--live fetches details/calendars/websites for venues the cache doesn't cover.
--claude asks Claude (web search) for an email when the website has none.
Check the emails by eye before a demo: scraping sometimes picks up the wrong address.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
FINDER = ROOT.parent / "sfparkandrec"
sys.path.insert(0, str(FINDER))

from resy_finder import contacts, resy  # noqa: E402
from resy_finder.http import HttpClient  # noqa: E402

OUT = ROOT / "data" / "restaurants.json"
OUR_CACHE = ROOT / "scripts" / ".cache"
CENTER = (37.7816, -122.4628)  # 94118, Inner Richmond
PARTY = 10


class TwoCacheHttp(HttpClient):
    """Reads the finder's cache (ignoring its age) and ours; writes only ours."""

    def __init__(self, live: bool):
        super().__init__(cache_dir=OUR_CACHE, cache_ttl=10**10, min_interval=0.4)
        self.live = live
        self.theirs = HttpClient(cache_dir=FINDER / ".cache", cache_ttl=10**10)

    def _cache_get(self, key):
        return super()._cache_get(key) or self.theirs._cache_get(key)

    def request(self, method, url, **kw):
        if not self.live:
            import urllib.parse
            params = kw.get("params")
            full = url + (("&" if "?" in url else "?") + urllib.parse.urlencode(params) if params else "")
            data = json.dumps(kw["json_body"]).encode().decode() if kw.get("json_body") is not None else ""
            cached = self._cache_get(f"{method} {full} {data}")
            if cached is None:
                raise contacts.HttpError(0, full, "not cached (run with --live)")
            return cached
        return super().request(method, url, **kw)


def cached_search_hits() -> list[resy.Venue]:
    """Every venue from cached Resy searches, closest to 94118 first."""
    venues = {}
    for f in (FINDER / ".cache").glob("*.json"):
        try:
            entry = json.loads(f.read_text())
        except ValueError:
            continue
        if "venuesearch/search" in entry.get("key", ""):
            for v in resy.parse_search_hits(json.loads(entry["body"])):
                if v.latitude and 37.6 < v.latitude < 37.85:  # San Francisco only
                    venues[v.id] = v
    return sorted(venues.values(), key=lambda v: (v.latitude - CENTER[0]) ** 2 + (v.longitude - CENTER[1]) ** 2)


def takes_groups(client: resy.ResyClient, venue: resy.Venue) -> bool:
    """True if Resy shows any date in the next ~2 months with a table for PARTY."""
    for key in [k for k in calendar_keys if f"venue_id={venue.id}&num_seats={PARTY}&" in k]:
        if resy.available_days(resy.parse_calendar(json.loads(calendar_keys[key]))):
            return True
    if calendar_keys_for(venue.id):
        return False  # cached calendars say no
    start = date.today() + timedelta(days=7)
    try:
        return bool(resy.available_days(client.calendar(venue.id, PARTY, start, start + timedelta(days=60))))
    except Exception:
        return False


def calendar_keys_for(venue_id: int) -> list[str]:
    return [k for k in calendar_keys if f"venue_id={venue_id}&num_seats={PARTY}&" in k]


def clean_address(address: str) -> str:
    """Resy sometimes repeats the street: '708 Clement St 708 Clement Street, ...'."""
    street, _, rest = address.partition(", ")
    m = re.match(r"^(\d+)\s+(.*?)\s+\1\s+(.*)$", street)
    if m:
        street = f"{m.group(1)} {m.group(2)}"
    return f"{street}, {rest}" if rest else street


def best_email(venue: resy.Venue) -> tuple[str, str] | tuple[None, None]:
    """Prefer an address on the restaurant's own domain, then events/info/reservations ones."""
    site = urlparse(venue.website or "").netloc.lower().removeprefix("www.")
    site_root = ".".join(site.split(".")[-2:]) if site else ""

    def score(e: str) -> tuple:
        local, _, domain = e.partition("@")
        return (
            not (site_root and domain.endswith(site_root)),
            not any(w in local for w in ("event", "party", "private", "reserv", "book", "info", "hello", "hola")),
            len(e),
        )

    if not venue.emails:
        return None, None
    email = sorted(venue.emails, key=score)[0]
    return email, venue.email_sources.get(email, "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--claude", action="store_true")
    ap.add_argument("--max", type=int, default=15, help="how many restaurants to keep")
    args = ap.parse_args()

    http = TwoCacheHttp(args.live)
    client = resy.ResyClient(http)
    claude = None
    if args.claude:
        from resy_finder.claude_lookup import ClaudeEmailSearch
        claude = ClaudeEmailSearch(http)

    global calendar_keys
    calendar_keys = {}
    for f in (FINDER / ".cache").glob("*.json"):
        try:
            entry = json.loads(f.read_text())
        except ValueError:
            continue
        if "venue/calendar" in entry.get("key", ""):
            calendar_keys[entry["key"]] = entry["body"]

    kept = []
    for venue in cached_search_hits():
        if len(kept) >= args.max:
            break
        try:
            client.venue_details(venue)
        except Exception as e:
            print(f"  skip {venue.name}: no details ({e})")
            continue
        if not takes_groups(client, venue):
            print(f"  skip {venue.name}: no tables for {PARTY} on Resy")
            continue
        if not venue.emails and venue.website:
            info = contacts.find_contacts(http, venue.website)
            for e in info.emails:
                venue.add_email(e, "website")
        if not venue.emails and claude:
            for e, source in claude.find(venue.name, venue.address, venue.website).items():
                venue.add_email(e, f"Claude web search: {source}")
        email, source = best_email(venue)
        if not email:
            print(f"  skip {venue.name}: no email found")
            continue
        kept.append({
            "id": f"resy-{venue.id}",
            "resy_id": venue.id,
            "name": venue.name,
            "neighborhood": venue.neighborhood or "",
            "cuisine": venue.cuisine[:2],
            "price_range": venue.price_range,
            "rating": round(venue.rating, 1) if venue.rating else None,
            "lat": venue.latitude,
            "lng": venue.longitude,
            "address": clean_address(venue.address or ""),
            "phone": venue.phone or "",
            "website": venue.website or "",
            "email": email,
            "email_source": source,
            "photo": venue.images[0] if venue.images else "",
            "photos": venue.images[:4],
            "resy_url": venue.resy_url() or "",
            "large_party_message": venue.large_party_message or "",
            "description": (venue.description or "")[:300],
        })
        print(f"  keep {venue.name}: {email} ({source})")

    OUT.write_text(json.dumps(kept, indent=2))
    print(f"\nWrote {len(kept)} restaurants to {OUT.relative_to(ROOT)}. Check the emails before a demo.")


if __name__ == "__main__":
    main()
