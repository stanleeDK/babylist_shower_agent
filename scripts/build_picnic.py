"""Build data/picnic_sites.json and static/picnic/*.jpg: SF picnic areas that hold 10+ people.

Runs inside the ../actualsfpark project, whose SF Rec & Park client, photo matcher and
on-disk cache it reuses:

    cd ../actualsfpark && uv run python ../babylist_shower_agent/scripts/build_picnic.py

Searches from a few points around the city, keeps sites that have a photo, geocodes each
park once (OpenStreetMap, 1 request/second), and copies the resized photos into this app.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, time, timedelta
from pathlib import Path

from sf_picnic_finder.activenet import BOOKING_URL, ActiveNetClient, SearchQuery, summarize_detail
from sf_picnic_finder.app import park_name
from sf_picnic_finder.cache import DiskCache
from sf_picnic_finder.geocode import Geocoder, GeocodeError
from sf_picnic_finder.photos import PhotoFinder, facility_label

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "picnic_sites.json"
PHOTOS = ROOT / "static" / "picnic"
CACHE = Path.cwd() / ".cache"  # actualsfpark's cache

# Inner Richmond, Golden Gate Park east, Mission/Dolores, Marina, Sunset, Bernal
SEARCH_POINTS = [(37.7816, -122.4628), (37.7694, -122.4530), (37.7596, -122.4269),
                 (37.8037, -122.4368), (37.7535, -122.4910), (37.7410, -122.4150)]
PER_POINT = 8


def nice_name(raw: str) -> str:
    """'MTNLAKE - Picnic Area @ 10th Ave Meadow' -> 'Picnic Area at 10th Ave Meadow'."""
    name = facility_label(raw.strip())
    name = re.sub(r"^GGP\s*-\s*", "", name)
    name = re.sub(r"PicnicArea", "Picnic Area", name)
    name = re.sub(r"\s*@\s*", " at ", name)
    name = re.sub(r"(\d)\s*tbls?\b", r"\1 tables", name)
    name = re.sub(r"(\d)table\b", r"\1 table", name)
    name = re.sub(r"\s*:\s*", ", ", name)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return re.sub(r"\s+", " ", name).strip()


async def main() -> None:
    cache = DiskCache(CACHE)
    activenet, photos, geocoder = ActiveNetClient(cache), PhotoFinder(cache), Geocoder(cache)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    day = date.today() + timedelta(days=30)
    while day.weekday() != 5:  # a Saturday, so the search returns the usual weekend sites
        day += timedelta(days=1)

    items: dict[int, dict] = {}
    for lat, lng in SEARCH_POINTS:
        page = await activenet.search(SearchQuery(lat, lng, day, time(11), time(15), 10))
        kept = 0
        for item in page.items:
            if item["id"] in items or (item.get("max_capacity") or 0) < 10:
                continue
            items[item["id"]] = item
            kept += 1
            if kept >= PER_POINT:
                break

    park_coords: dict[str, tuple[float, float] | None] = {}
    sites = []
    for item in items.values():
        detail = await activenet.detail(item["id"])
        info = summarize_detail(detail)
        raw_name = (detail or {}).get("general_information", {}).get("facility_name") or item["name"]
        url = await photos.photo_url_for(raw_name, info["info_url"], info["thumbnail_url"])
        image = await photos.fetch_image(url) if url else None
        if not image:
            print(f"  skip {item['name']}: no photo")
            continue
        park = park_name(item.get("center_name") or "")
        if park not in park_coords:
            try:
                loc = await geocoder.geocode(f"{park}, San Francisco")
                park_coords[park] = (loc.latitude, loc.longitude)
            except GeocodeError:
                park_coords[park] = None
        if not park_coords[park]:
            print(f"  skip {item['name']}: couldn't place {park} on the map")
            continue
        data, content_type = image
        ext = ".png" if "png" in content_type else ".jpg"
        (PHOTOS / f"{item['id']}{ext}").write_bytes(data)
        sites.append({
            "id": f"picnic-{item['id']}",
            "facility_id": item["id"],
            "name": nice_name(raw_name),
            "park": park,
            "capacity": item.get("max_capacity"),
            "booking_type": (item.get("type_name") or "").replace("Picnic Area - ", ""),
            "lat": park_coords[park][0],
            "lng": park_coords[park][1],
            "address": info["address"],
            "amenities": info["amenities"][:5],
            "photo": f"/static/picnic/{item['id']}{ext}",
            "book_url": BOOKING_URL.format(facility_id=item["id"]),
            "info_url": info["info_url"],
        })
        print(f"  keep {sites[-1]['name']} ({park}, {sites[-1]['capacity']} people)")

    OUT.write_text(json.dumps(sites, indent=2))
    print(f"\nWrote {len(sites)} picnic sites to {OUT.relative_to(ROOT)}")
    for c in (activenet, photos, geocoder):
        await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
