"""Shopping list: Claude plans ~10 items for the shower, Kroger finds them at the nearest store.

Mirrors the Kroger prototype (../instacart): plan with Claude -> search every item at the
store at once -> one quick Claude pass picks the best product per item -> one retry search
for items with no good match. The organizer gets product photos and prices, can edit the
list (here or by asking the agent), and schedules delivery for the morning of the shower.

Kroger's API can't schedule or place orders, so delivery is our own schedule: a couple of
days before the shower the worker wakes up, the agent asks the organizer to confirm
payment, and the order is placed (simulated in this demo: nothing is charged). A Google
Calendar reminder marks the delivery window on the shower day.

The list is stored in the `shopping` table and written in stages (planning -> searching
-> ready), so the page fills in while it's built.
"""
from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import anthropic
import httpx
from pydantic import BaseModel, Field

import config
import db

API = "https://api.kroger.com/v1"
SITE = "https://www.kroger.com"
ITEMS = 10
MATCHES_PER_ITEM = 6

client = anthropic.Anthropic()

Category = Literal["Food", "Snacks", "Desserts", "Drinks", "Produce", "Tableware", "Decorations", "Other"]


class PlannedItem(BaseModel):
    search_term: str = Field(description="Short, generic grocery-store search term, 1-3 words, e.g. 'paper plates'.")
    display_name: str = Field(description="Friendly label, e.g. 'Fruit platter'.")
    category: Category
    quantity: int = Field(description="Number of store packages to buy, 1-24.")
    reason: str = Field(description="One short sentence: why it's on the list and how much.")


class PlannedList(BaseModel):
    title: str = Field(description="Short title, e.g. 'Baby shower picnic for 8'.")
    summary: str = Field(description="One sentence summarizing the plan.")
    items: list[PlannedItem]


class MatchChoice(BaseModel):
    item_number: int
    best_candidate: int = Field(description="Candidate number of the best fit, or -1 if none fit.")
    better_search_term: str = Field(description="If none fit, a different short search term; otherwise ''.")


class MatchChoices(BaseModel):
    choices: list[MatchChoice]


PLAN_PROMPT = f"""\
You plan shopping lists for baby showers. Each item is searched for at a Kroger-family
grocery store, and the organizer reviews the products before buying.

Make a list of exactly {ITEMS} items that covers the essentials: food, drinks, a dessert,
tableware, and one or two decorations a grocery store carries. Fit it to the setting:
- At home: platters and finger food, drinks, cake or cupcakes, plates, cups, napkins,
  balloons or a tablecloth.
- A park picnic: food that travels and needs no kitchen (sandwiches, fruit, chips,
  cookies), drinks in cans or bottles, ice, a disposable tablecloth, plates, napkins,
  trash bags.
Scale quantities to the headcount. `search_term` is typed into the store's search box, so
use one or two plain catalog words ("veggie tray", "lemonade", "napkins", "balloons",
"ice"), not themed descriptions; put the detail in `display_name`. Avoid alcohol. Don't
duplicate items."""

MATCH_PROMPT = """\
You pick grocery products for a baby shower shopping list. For each list item you get the
store's search results as numbered candidates. Choose the candidate that best fits the item
and a baby shower (avoid "Happy Birthday", holiday or adult themes; a tablecloth is a party
table cover, not a dish cloth). The product must be the same kind of thing as the item:
bagged ice is not iced tea, and a fruit platter is not a bag of apples. Prefer candidates
with a price. If none is a real fit, set best_candidate to -1 and suggest a better short
search term. One choice per item."""


# ---------------- Kroger ----------------


class KrogerError(Exception):
    pass


_token: dict = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()
_stores: dict[str, dict | None] = {}


def configured() -> bool:
    return bool(config.KROGER_CLIENT_ID and config.KROGER_CLIENT_SECRET)


def _headers() -> dict:
    with _token_lock:
        if not _token["value"] or time.time() >= _token["expires"]:
            if not configured():
                raise KrogerError("Add KROGER_CLIENT_ID and KROGER_CLIENT_SECRET to .env to build shopping lists.")
            resp = httpx.post(f"{API}/connect/oauth2/token", auth=(config.KROGER_CLIENT_ID, config.KROGER_CLIENT_SECRET),
                              data={"grant_type": "client_credentials", "scope": "product.compact"}, timeout=15)
            if resp.status_code != 200:
                raise KrogerError(f"Kroger sign-in failed ({resp.status_code}).")
            tok = resp.json()
            _token["value"], _token["expires"] = tok["access_token"], time.time() + tok.get("expires_in", 1800) - 60
        return {"Authorization": f"Bearer {_token['value']}", "Accept": "application/json"}


def _get(path: str, params: dict) -> dict:
    try:
        resp = httpx.get(f"{API}{path}", params=params, headers=_headers(), timeout=15)
    except httpx.HTTPError as e:
        raise KrogerError(f"Couldn't reach Kroger: {e}") from e
    if resp.status_code == 429:
        raise KrogerError("Kroger rate limit hit. Try again shortly.")
    if resp.status_code >= 400:
        raise KrogerError(f"Kroger error {resp.status_code}")
    return resp.json()


def nearest_store(zip_code: str) -> dict | None:
    """The closest Kroger-family store, falling back to the default zip. Cached per zip."""
    for z in dict.fromkeys([zip_code, config.DEFAULT_ZIP]):
        if z not in _stores:
            data = _get("/locations", {"filter.zipCode.near": z, "filter.limit": 1}).get("data", [])
            s = data[0] if data else None
            _stores[z] = s and {
                "location_id": s["locationId"],
                "name": s.get("name", ""),
                "chain": s.get("chain", ""),
                "address": ", ".join(x for x in [(s.get("address") or {}).get("addressLine1"),
                                                  (s.get("address") or {}).get("city")] if x),
                "zip": z,
            }
        if _stores[z]:
            return _stores[z]
    return None


def search_products(term: str, location_id: str, limit: int = 8) -> list[dict]:
    data = _get("/products", {"filter.term": term, "filter.locationId": location_id, "filter.limit": limit})
    products = [p for p in (_parse_product(d) for d in data.get("data", [])) if p]
    return sorted(products, key=lambda p: (p["price"] is None, p["stock"] == "TEMPORARILY_OUT_OF_STOCK"))


def _parse_product(d: dict) -> dict | None:
    item = (d.get("items") or [{}])[0]
    upc = d.get("upc") or item.get("itemId")
    if not upc:
        return None
    price = item.get("price") or {}
    regular, promo = price.get("regular") or None, price.get("promo") or None
    fulfillment = item.get("fulfillment") or {}
    uri = d.get("productPageURI")
    return {
        "upc": upc,
        "description": d.get("description", ""),
        "brand": d.get("brand", ""),
        "size": item.get("size", ""),
        "price": regular,
        "promo_price": promo if promo and regular and promo < regular else None,
        "image": _pick_image(d.get("images") or []),
        "stock": (item.get("inventory") or {}).get("stockLevel"),
        "url": f"{SITE}{uri}" if uri else None,
        "pickup": bool(fulfillment.get("curbside") or fulfillment.get("inStore")),
        "delivery": bool(fulfillment.get("delivery")),
    }


def _pick_image(images: list[dict]) -> str | None:
    ordered = sorted(images, key=lambda im: (not im.get("featured"), im.get("perspective") != "front"))
    for im in ordered:
        sizes = {s.get("size"): s.get("url") for s in im.get("sizes", [])}
        for size in ("medium", "large", "small", "xlarge", "thumbnail"):
            if sizes.get(size):
                return sizes[size]
    return None


# ---------------- Claude ----------------


def plan(setting: str, headcount: int, details: str) -> PlannedList:
    where = "a park picnic" if setting == "picnic" else "the host's home"
    response = client.messages.parse(
        model=config.LIST_MODEL,
        max_tokens=8000,
        system=PLAN_PROMPT,
        messages=[{"role": "user", "content": f"A baby shower at {where} for {headcount} people. {details}"}],
        output_format=PlannedList,
        output_config={"effort": "low"},
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError("Couldn't plan a list this time.")
    out = response.parsed_output
    out.items = out.items[:ITEMS]
    return out


def pick_matches(items: list[tuple[str, list[str]]]) -> dict[int, MatchChoice]:
    """Best effort: {} on any failure, and callers keep Kroger's own ranking."""
    lines = []
    for n, (label, candidates) in enumerate(items):
        if candidates:
            lines.append(f"Item {n}: {label}")
            lines += [f"  {c}. {desc}" for c, desc in enumerate(candidates)]
    if not lines:
        return {}
    try:
        response = client.messages.parse(
            model=config.FAST_MODEL,
            max_tokens=4000,
            system=MATCH_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=MatchChoices,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
        )
    except (anthropic.APIError, TypeError):
        return {}
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        return {}
    return {c.item_number: c for c in response.parsed_output.choices}


def _describe(p: dict) -> str:
    price = f"${p['price']:.2f}" if p["price"] else "not sold at this store"
    return f"{p['description']} ({p['size']}) - {price}"


# ---------------- building the list ----------------


def start(event_id: str, setting: str, headcount: int, details: str, zip_code: str, log=None) -> None:
    """Build the list in the background. `log(text)` reports progress to the activity feed."""
    db.update_shopping(event_id, lambda _: {"status": "planning", "setting": setting, "headcount": headcount,
                                            "items": [], "store": None, "error": "", "delivery": None})
    threading.Thread(target=_build, args=(event_id, setting, headcount, details, zip_code, log or (lambda t: None)),
                     daemon=True, name="shopping").start()


def _build(event_id: str, setting: str, headcount: int, details: str, zip_code: str, log) -> None:
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(2) as pool:  # find the store while Claude plans
            store_job = pool.submit(nearest_store, zip_code)
            planned = plan(setting, headcount, details)
            store = store_job.result()
        if not store:
            raise KrogerError("No Kroger-family store found nearby.")
        log(f"Shopping list: planned {len(planned.items)} items with Claude; searching {store['name']} "
            f"({time.monotonic() - started:.1f}s)")

        def staged(data):
            if data is None:
                return None
            data.update(status="searching", title=planned.title, summary=planned.summary, store=store,
                        items=[{**i.model_dump(), "matches": [], "chosen": 0, "removed": False} for i in planned.items])
            return data
        if db.update_shopping(event_id, staged) is None:
            return  # reset while building

        t = time.monotonic()
        with ThreadPoolExecutor(ITEMS) as pool:
            results = list(pool.map(lambda i: _safe_search(i.search_term, store["location_id"]), planned.items))
        searched = time.monotonic() - t
        choices = pick_matches([(f"{i.display_name} ({i.search_term})", [_describe(p) for p in r])
                                for i, r in zip(planned.items, results)])
        retry = {n: c.better_search_term for n, c in choices.items()
                 if c.best_candidate < 0 and c.better_search_term and n < len(results)}
        with ThreadPoolExecutor(max(1, len(retry))) as pool:
            retried = dict(zip(retry, pool.map(lambda term: _safe_search(term, store["location_id"], 6), retry.values())))
        for n, r in enumerate(results):
            c = choices.get(n)
            if n in retried:
                r = retried[n]
            elif c and 0 <= c.best_candidate < len(r):
                r = [r[c.best_candidate]] + [p for k, p in enumerate(r) if k != c.best_candidate]
            results[n] = r[:MATCHES_PER_ITEM]

        def finished(data):
            if data is None:
                return None
            for item, r in zip(data["items"], results):
                item["matches"] = r
            data["status"] = "ready"
            return data
        db.update_shopping(event_id, finished)
        found = sum(bool(r) for r in results)
        log(f"Shopping list: found {found} of {len(results)} items at {store['name']}: Kroger searches {searched:.1f}s, "
            f"total {time.monotonic() - started:.1f}s")
    except Exception as e:
        traceback.print_exc()
        message = str(e) if isinstance(e, KrogerError) else "Couldn't build the list this time."
        db.update_shopping(event_id, lambda d: d and {**d, "status": "error", "error": message})
        log(f"Shopping list failed: {e}")


def _safe_search(term: str, location_id: str, limit: int = 8) -> list[dict]:
    try:
        return search_products(term, location_id, limit)
    except KrogerError:
        return []


# ---------------- the organizer's edits ----------------


def edit(event_id: str, action: str, item: int | None = None, value=None) -> dict | None:
    """The organizer's edits from the Shopping list tab."""
    if action == "add":
        added = add_items(event_id, [{"name": str(value or "").strip()[:60], "quantity": 1}])
        return db.get_shopping(event_id) if added else None
    if action == "schedule":
        return schedule_delivery(event_id, str(value or ""))
    if action == "cancel_delivery":
        return db.update_shopping(event_id, lambda d: {**d, "delivery": {**d["delivery"], "status": "cancelled"}}
                                  if d and d.get("delivery") else None)

    def change(data):
        if not data or data.get("status") != "ready":
            return None
        items = data["items"]
        if item is not None and 0 <= item < len(items):
            it = items[item]
            if action == "remove":
                it["removed"] = True
            elif action == "restore":
                it["removed"] = False
            elif action == "swap" and isinstance(value, int) and 0 <= value < len(it["matches"]):
                it["chosen"] = value
            elif action == "qty" and isinstance(value, int):
                it["quantity"] = max(1, min(24, value))
            else:
                return None
        else:
            return None
        return data
    return db.update_shopping(event_id, change)


# ---------------- edits by name (the agent, or the "add an item" box) ----------------


def _find(items: list[dict], name: str) -> int | None:
    name = name.strip().lower()
    for n, it in enumerate(items):
        if not it.get("removed") and (it["display_name"].lower() == name or it["search_term"].lower() == name):
            return n
    for n, it in enumerate(items):  # looser: "milk" finds "Chocolate milk"
        if not it.get("removed") and (name in it["display_name"].lower() or it["display_name"].lower() in name):
            return n
    return None


def add_items(event_id: str, items: list[dict]) -> list[str]:
    """Add items ({"name", "search_term"?, "quantity"?}), searching Kroger for each. Returns
    what was added, e.g. ["Chocolate milk (TruMoo Chocolate Milk, $3.49)"]."""
    data = db.get_shopping(event_id)
    if not data or data.get("status") != "ready" or not data.get("store"):
        return []
    new = []
    for raw in items[:10]:
        name = str(raw.get("name") or "").strip()[:60]
        if not name:
            continue
        term = str(raw.get("search_term") or name).strip()[:40]
        matches = _safe_search(term, data["store"]["location_id"], 6)
        new.append({"search_term": term, "display_name": name[:1].upper() + name[1:], "category": "Other",
                    "quantity": max(1, min(24, int(raw.get("quantity") or 1))), "reason": "Added by you",
                    "matches": matches[:MATCHES_PER_ITEM], "chosen": 0, "removed": False})
    if not new:
        return []
    db.update_shopping(event_id, lambda d: d and {**d, "items": d["items"] + new})
    return [f"{it['display_name']} ({it['matches'][0]['description']}"
            f"{', $%.2f' % it['matches'][0]['price'] if it['matches'] and it['matches'][0]['price'] else ''})"
            if it["matches"] else f"{it['display_name']} (no match at this store)" for it in new]


def remove_items(event_id: str, names: list[str]) -> list[str]:
    removed = []

    def change(d):
        if not d:
            return None
        for name in names:
            n = _find(d["items"], name)
            if n is not None:
                d["items"][n]["removed"] = True
                removed.append(d["items"][n]["display_name"])
        return d
    db.update_shopping(event_id, change)
    return removed


def set_quantity(event_id: str, name: str, quantity: int) -> str | None:
    found = []

    def change(d):
        n = _find(d["items"], name) if d else None
        if n is None:
            return None
        d["items"][n]["quantity"] = max(1, min(24, quantity))
        found.append(d["items"][n]["display_name"])
        return d
    db.update_shopping(event_id, change)
    return found[0] if found else None


# ---------------- delivery ----------------


def delivery_slots(start_time: str | None) -> list[str]:
    """Two-hour delivery windows on the shower day that end at least an hour before it starts."""
    start = int((start_time or "13:00")[:2])
    slots = [f"{h:02d}:00-{h + 2:02d}:00" for h in range(7, 20) if h + 2 <= start - 1]
    return slots[-3:] or ["07:00-09:00"]


def slot_label(slot: str) -> str:
    from chat import nice_time

    a, b = slot.split("-")
    return f"{nice_time(a)}–{nice_time(b)}"


def schedule_delivery(event_id: str, slot: str) -> dict | None:
    """Ask for delivery in a window on the shower day. The worker sets the dates, adds the
    calendar reminder, and wakes up to order closer to the day."""
    if not (len(slot) == 11 and slot[2] == ":" and slot[5] == "-"):
        return None
    def change(d):
        if not d or d.get("status") != "ready":
            return None
        old = d.get("delivery") or {}
        # Keep the calendar reminder if there is one: the worker moves it to the new window.
        return {**d, "delivery": {"status": "scheduled", "slot": slot, "date": None, "order_at": None,
                                  "reminder_id": old.get("reminder_id")}}
    return db.update_shopping(event_id, change)


def totals(data: dict) -> dict:
    """Estimated total, and how many chosen products the store can't deliver."""
    total, unavailable = 0.0, 0
    for it in data.get("items", []):
        if it.get("removed") or not it.get("matches"):
            continue
        p = it["matches"][it.get("chosen", 0)]
        price = p.get("promo_price") or p.get("price")
        if price:
            total += price * it.get("quantity", 1)
        if not p.get("delivery"):
            unavailable += 1
    return {"total": round(total, 2), "unavailable": unavailable}


def summary_for_agent(data: dict | None) -> dict | str:
    if not data:
        return "not requested"
    items = [f"{it['quantity']} x {it['display_name']}" + ("" if it["matches"] else " (no match at the store)")
             for it in data.get("items", []) if not it.get("removed")]
    return {"status": data.get("status"), "items": items, "delivery": data.get("delivery")}
