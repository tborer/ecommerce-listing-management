#!/usr/bin/env python3
"""eBay public-data API discovery: Deal API, Browse API, Taxonomy API.

Replacement for scraping eBay Deals/search pages with a browser
(pipeline.py's EBAY_TILE_JS / EBAY_SEARCH_TILE_JS) -- web-app-plan.md §4a,
migration step 1. Everything here is read-only public data, authenticated
with an *application* token (ebay.auth.get_application_token -- app keys
only, no seller consent, no account/inventory/order access).

Output shape is deliberately the same {id, title, priceNum, url} tile dict
the scrapers return (ApiItem.to_tile()), with `id` = the legacy numeric item
id (what `/itm/<id>` and the scrapers' `data-listing-id` use) -- so every
downstream step (EbayCandidate, DSers search, matching, SKU = branch11-<id>)
is unaware which discovery source produced a tile.

Deals pages -> API mapping: DEALS_PAGE_CATEGORIES maps each eBay Deals URL
in pipeline.DEALS_URL_POOL to eBay category *names* (optionally a
" > "-separated path suffix to disambiguate). Names are resolved to real
category ids at runtime from the live EBAY_US category tree (Taxonomy API,
cached to disk) rather than hardcoded ids -- run `categories` below to see
exactly what each URL resolves to, and fix any name that resolves to nothing.

Usage (production, local credential file by default):
    python -m ecommerce_listing_mgmt.ebay.browse token-check
    python -m ecommerce_listing_mgmt.ebay.browse categories
    python -m ecommerce_listing_mgmt.ebay.browse deals [--category-id 1281] [--max 50]
    python -m ecommerce_listing_mgmt.ebay.browse events
    python -m ecommerce_listing_mgmt.ebay.browse search "mouse pad" [--category-id 31530] [--price-max 100]
    python -m ecommerce_listing_mgmt.ebay.browse item 395766041202
    python -m ecommerce_listing_mgmt.ebay.browse url https://www.ebay.com/deals/home-garden/pet-supplies
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from ecommerce_listing_mgmt.ebay.auth import api_base, get_application_token

MARKETPLACE_ID = "EBAY_US"

# Page-size caps per the API docs: Deal API getDealItems/getEventItems max
# 100 per call, Browse search max 200 per call (and offset+limit <= 10,000).
DEAL_PAGE_SIZE = 100
BROWSE_PAGE_SIZE = 200

# Cache for the flattened category tree. Defaults next to this module like
# the rest of the package's state files; ELM_DATA_DIR overrides it (the
# web-app-plan Phase 0 direction for all state files).
DATA_DIR = Path(os.environ.get("ELM_DATA_DIR") or Path(__file__).parent)
CATEGORY_CACHE_PATH = DATA_DIR / "ebay_category_tree_EBAY_US.json"
CATEGORY_CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600

# eBay Deals page URL -> category names/path-suffixes it corresponds to.
# Resolved against the live tree by resolve_category_ids(); a name that
# matches several nodes (e.g. "Sunglasses" under both Men's and Women's
# accessories) deliberately includes all of them. Verify with the
# `categories` CLI command before relying on a mapping -- these names were
# chosen from eBay US's category structure without live access to the tree.
DEALS_PAGE_CATEGORIES: dict[str, list[str]] = {
    "https://www.ebay.com/deals/tech/computer-accessories": [
        "Laptop & Desktop Accessories", "Keyboards, Mice & Pointers"],
    "https://www.ebay.com/deals/home-garden/kitchen-dining-bar": ["Kitchen, Dining & Bar"],
    "https://www.ebay.com/deals/home-garden/tools": ["Home & Garden > Tools & Workshop Equipment"],
    "https://www.ebay.com/deals/home-garden/pet-supplies": ["Pet Supplies"],
    "https://www.ebay.com/deals/trending/home-garden/crafts": ["Crafts"],
    "https://www.ebay.com/deals/trending/other-deals/office-furniture-supplies": [
        "Office Furniture", "Office Supplies"],
    "https://www.ebay.com/deals/trending/home-garden/yard-garden-outdoor-living": [
        "Yard, Garden & Outdoor Living"],
    "https://www.ebay.com/deals/trending/automotive/car-accessories": [
        "Car & Truck Parts & Accessories > Interior Parts & Accessories",
        "Car & Truck Parts & Accessories > Exterior Parts & Accessories"],
    "https://www.ebay.com/deals/trending/home-garden/home-improvement": ["Home Improvement"],
    "https://www.ebay.com/deals/trending/fashion/sunglasses": ["Sunglasses"],
    "https://www.ebay.com/deals/trending/home-garden/lamps-lighting-ceiling-fans": [
        "Lamps, Lighting & Ceiling Fans"],
    "https://www.ebay.com/deals/trending/home-garden/home-organization": ["Home Organization"],
    "https://www.ebay.com/deals/trending/tech/memory-drives-storage": ["Drives, Storage & Blank Media"],
    "https://www.ebay.com/deals/trending/tech/headphones-portable-audio": ["Headphones"],
    "https://www.ebay.com/deals/trending/sporting-goods/exercise-fitness": ["Fitness, Running & Yoga"],
}


class EbayApiError(RuntimeError):
    def __init__(self, status: int, path: str, body: object):
        self.status, self.path, self.body = status, path, body
        super().__init__(f"eBay API {path} -> HTTP {status}: {_error_detail(body)}")


def _error_detail(body: object) -> str:
    if isinstance(body, dict) and body.get("errors"):
        return "; ".join(
            f"{e.get('errorId')}: {e.get('longMessage') or e.get('message')}" for e in body["errors"])
    return str(body)[:500]


# Module-level so unit tests can swap in a fake transport.
_urlopen = urllib.request.urlopen

# Per-process call counter, printed by the CLI -- Browse/Deal calls count
# against the app's shared daily quota (see web-app-plan.md §4a).
CALL_COUNT = {"n": 0}


def _get(path: str, params: dict | None = None, env: str = "production",
         credential_mode: str = "local") -> dict:
    """GET an eBay REST path with an application token. Raises EbayApiError
    on any non-2xx, with eBay's own error text -- never a guessed cause."""
    token = get_application_token(env, credential_mode)
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{api_base(env)}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
        "Accept": "application/json",
    })
    CALL_COUNT["n"] += 1
    try:
        with _urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body: object = json.loads(raw)
        except json.JSONDecodeError:
            body = raw.decode(errors="replace")
        raise EbayApiError(e.code, path, body) from None


# ---------------------------------------------------------------------------
# Normalized item
# ---------------------------------------------------------------------------

@dataclass
class ApiItem:
    id: str                          # legacy numeric item id, same as the scrapers' tile id
    title: str
    priceNum: float
    currency: str
    url: str
    source: str                      # "deal" | "event" | "browse"
    image_url: str | None = None
    category_id: str | None = None
    original_price: float | None = None
    discount_pct: float | None = None
    item_group_id: str | None = None  # set for multi-variation listings (see Item 25)

    def to_tile(self) -> dict:
        """The exact {id, title, priceNum, url} shape pipeline.EbayCandidate takes."""
        return {"id": self.id, "title": self.title, "priceNum": self.priceNum, "url": self.url}


def _legacy_id(raw: dict) -> str | None:
    """Deal/Browse items carry `legacyItemId` directly; Browse's RESTful
    `itemId` is `v1|<legacy>|<variation>` if that's all we have."""
    if raw.get("legacyItemId"):
        return str(raw["legacyItemId"])
    item_id = raw.get("itemId") or ""
    parts = item_id.split("|")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


def _money(obj: object) -> float | None:
    if isinstance(obj, dict) and obj.get("value") is not None:
        try:
            return float(obj["value"])
        except (TypeError, ValueError):
            return None
    return None


def parse_item(raw: dict, source: str) -> ApiItem | None:
    """Normalize one dealItem / eventItem / itemSummary. Returns None for
    anything without a usable id, title, and USD price (the scrapers drop
    the same cases)."""
    legacy = _legacy_id(raw)
    title = (raw.get("title") or "").strip()
    price = _money(raw.get("price"))
    currency = (raw.get("price") or {}).get("currency") or "USD"
    if not legacy or not title or price is None or currency != "USD":
        return None
    mp = raw.get("marketingPrice") or {}
    discount = mp.get("discountPercentage")
    try:
        discount_pct = float(discount) if discount is not None else None
    except (TypeError, ValueError):
        discount_pct = None
    return ApiItem(
        id=legacy, title=title, priceNum=price, currency=currency,
        url=f"https://www.ebay.com/itm/{legacy}", source=source,
        image_url=(raw.get("image") or {}).get("imageUrl"),
        category_id=str(raw["categoryId"]) if raw.get("categoryId") else (
            str(raw["categories"][0]["categoryId"]) if raw.get("categories") else None),
        original_price=_money(mp.get("originalPrice")),
        discount_pct=discount_pct,
        item_group_id=raw.get("itemGroupId") or (raw.get("primaryItemGroup") or {}).get("itemGroupId"),
    )


def _paginate(path: str, params: dict, list_key: str, page_size: int, max_items: int,
              source: str, env: str, credential_mode: str) -> list[ApiItem]:
    items: list[ApiItem] = []
    offset = 0
    while len(items) < max_items:
        limit = min(page_size, max_items - len(items))
        data = _get(path, {**params, "limit": limit, "offset": offset}, env, credential_mode)
        page = data.get(list_key) or []
        for raw in page:
            parsed = parse_item(raw, source)
            if parsed:
                items.append(parsed)
        offset += len(page)
        total = data.get("total")
        if not page or not data.get("next") or (total is not None and offset >= total):
            break
    return items[:max_items]


# ---------------------------------------------------------------------------
# Deal API
# ---------------------------------------------------------------------------

def get_deal_items(category_ids: list[str] | None = None, max_items: int = 100,
                   env: str = "production", credential_mode: str = "local") -> list[ApiItem]:
    """Deal API getDealItems -- eBay's own curated deals, the structured
    equivalent of the /deals pages. `category_ids` narrows to those
    categories (comma-joined, per the API)."""
    params = {"category_ids": ",".join(category_ids) if category_ids else None}
    return _paginate("/buy/deal/v1/deal_item", params, "dealItems", DEAL_PAGE_SIZE,
                     max_items, "deal", env, credential_mode)


def get_events(env: str = "production", credential_mode: str = "local") -> list[dict]:
    """Deal API getEvents -- current eBay promotional events (raw dicts)."""
    data = _get("/buy/deal/v1/event", {"limit": 100}, env, credential_mode)
    return data.get("events") or []


def get_event_items(event_ids: list[str], category_ids: list[str] | None = None,
                    max_items: int = 100, env: str = "production",
                    credential_mode: str = "local") -> list[ApiItem]:
    params = {"event_ids": ",".join(event_ids),
              "category_ids": ",".join(category_ids) if category_ids else None}
    return _paginate("/buy/deal/v1/event_item", params, "eventItems", DEAL_PAGE_SIZE,
                     max_items, "event", env, credential_mode)


# ---------------------------------------------------------------------------
# Browse API
# ---------------------------------------------------------------------------

def build_browse_filter(price_max: float | None = None, fixed_price_only: bool = True,
                        conditions: list[str] | None = None) -> str | None:
    """Browse `filter` string. Price filter needs priceCurrency alongside it."""
    parts = []
    if price_max is not None:
        parts.append(f"price:[..{price_max:g}]")
        parts.append("priceCurrency:USD")
    if fixed_price_only:
        parts.append("buyingOptions:{FIXED_PRICE}")
    if conditions:
        parts.append("conditions:{" + "|".join(conditions) + "}")
    return ",".join(parts) or None


def search_items(q: str | None = None, category_id: str | None = None,
                 price_max: float | None = None, fixed_price_only: bool = True,
                 conditions: list[str] | None = None, sort: str | None = None,
                 max_items: int = 100, env: str = "production",
                 credential_mode: str = "local") -> list[ApiItem]:
    """Browse API item_summary/search. Needs `q` and/or `category_id`
    (Browse accepts a single category id per request). `conditions`
    defaults to None (any) -- pass ["NEW"] to mirror new-only sourcing."""
    if not q and not category_id:
        raise ValueError("search_items needs q and/or category_id")
    params = {"q": q, "category_ids": category_id,
              "filter": build_browse_filter(price_max, fixed_price_only, conditions),
              "sort": sort}
    return _paginate("/buy/browse/v1/item_summary/search", params, "itemSummaries",
                     BROWSE_PAGE_SIZE, max_items, "browse", env, credential_mode)


def get_item_by_legacy_id(legacy_id: str, env: str = "production",
                          credential_mode: str = "local") -> dict:
    """Full Browse getItemByLegacyId payload (includes
    estimatedAvailabilities[].estimatedSoldQuantity -- a demand signal).
    Raises EbayApiError; a multi-variation listing needs a variation id
    (error 11006) -- use get_items_by_group() for those."""
    return _get("/buy/browse/v1/item/get_item_by_legacy_id",
                {"legacy_item_id": legacy_id}, env, credential_mode)


def get_items_by_group(item_group_id: str, env: str = "production",
                       credential_mode: str = "local") -> dict:
    return _get("/buy/browse/v1/item/get_items_by_item_group",
                {"item_group_id": item_group_id}, env, credential_mode)


def estimated_sold_quantity(item: dict) -> int | None:
    """Sum of estimatedSoldQuantity across a getItem payload's availabilities."""
    total, seen = 0, False
    for av in item.get("estimatedAvailabilities") or []:
        if av.get("estimatedSoldQuantity") is not None:
            total += int(av["estimatedSoldQuantity"])
            seen = True
    return total if seen else None


# ---------------------------------------------------------------------------
# Taxonomy API -- category name -> id resolution
# ---------------------------------------------------------------------------

def _flatten_tree(node: dict, path: tuple[str, ...] = ()) -> list[dict]:
    cat = node.get("category") or {}
    name = cat.get("categoryName")
    here = path + (name,) if name and name != "Root" else path
    out = []
    if name and name != "Root":
        out.append({"id": str(cat.get("categoryId")), "name": name, "path": list(here)})
    for child in node.get("childCategoryTreeNodes") or []:
        out.extend(_flatten_tree(child, here))
    return out


def fetch_category_tree(env: str = "production", credential_mode: str = "local") -> list[dict]:
    tree_id = _get("/commerce/taxonomy/v1/get_default_category_tree_id",
                   {"marketplace_id": MARKETPLACE_ID}, env, credential_mode)["categoryTreeId"]
    tree = _get(f"/commerce/taxonomy/v1/category_tree/{tree_id}", None, env, credential_mode)
    return _flatten_tree(tree.get("rootCategoryNode") or {})


def load_category_tree(env: str = "production", credential_mode: str = "local",
                       refresh: bool = False) -> list[dict]:
    """Flattened [{id, name, path}] for EBAY_US, cached on disk for 30 days
    (the full tree is large and changes rarely)."""
    if not refresh and CATEGORY_CACHE_PATH.exists():
        cached = json.loads(CATEGORY_CACHE_PATH.read_text())
        if time.time() - cached.get("fetched_at", 0) < CATEGORY_CACHE_MAX_AGE_SECONDS:
            return cached["categories"]
    categories = fetch_category_tree(env, credential_mode)
    CATEGORY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATEGORY_CACHE_PATH.write_text(json.dumps({"fetched_at": time.time(), "categories": categories}))
    return categories


def resolve_category_ids(specs: list[str], categories: list[dict]) -> dict[str, list[str]]:
    """Each spec is a category name, or a " > "-separated path suffix
    ("Car & Truck Parts & Accessories > Interior Parts & Accessories").
    Case-insensitive exact match on the trailing path segments. Returns
    {spec: [ids]} -- an empty list means the spec matched nothing and needs
    fixing, never a silent guess."""
    out: dict[str, list[str]] = {}
    for spec in specs:
        want = [s.strip().lower() for s in spec.split(">")]
        out[spec] = [c["id"] for c in categories
                     if [p.lower() for p in c["path"][-len(want):]] == want]
    return out


def category_ids_for_deals_url(url: str, categories: list[dict]) -> list[str] | None:
    """None if the URL has no API mapping at all (caller decides what to do);
    otherwise the de-duplicated resolved ids (possibly empty)."""
    specs = DEALS_PAGE_CATEGORIES.get(url)
    if specs is None:
        return None
    ids: list[str] = []
    for spec_ids in resolve_category_ids(specs, categories).values():
        ids.extend(i for i in spec_ids if i not in ids)
    return ids


# ---------------------------------------------------------------------------
# Discovery entry points used by pipeline.py
# ---------------------------------------------------------------------------

def discover_deals_url(url: str, max_items: int, price_ceiling: float | None = None,
                       env: str = "production", credential_mode: str = "local",
                       browse_fallback: bool = True) -> list[ApiItem]:
    """API equivalent of scraping one eBay Deals page: Deal API items in the
    page's mapped categories. If the Deal API returns nothing (or refuses --
    it's documented as limited release), fall back to Browse category
    searches kept to discounted items only (marketingPrice present), which
    is the closest public equivalent of "a deal". Raises KeyError if `url`
    has no DEALS_PAGE_CATEGORIES mapping; returns [] (with a log line) if
    its category names don't resolve."""
    categories = load_category_tree(env, credential_mode)
    ids = category_ids_for_deals_url(url, categories)
    if ids is None:
        raise KeyError(f"no DEALS_PAGE_CATEGORIES mapping for {url}")
    if not ids:
        print(f"[browse] {url}: none of {DEALS_PAGE_CATEGORIES[url]} resolved to a category id "
              f"-- fix the mapping (run the `categories` command)")
        return []

    items: list[ApiItem] = []
    try:
        items = get_deal_items(ids, max_items=max_items, env=env, credential_mode=credential_mode)
    except EbayApiError as e:
        if not browse_fallback:
            raise
        print(f"[browse] Deal API failed for {url} ({e}) -- falling back to Browse")
    if items:
        if price_ceiling is not None:
            items = [i for i in items if i.priceNum < price_ceiling]
        return items[:max_items]
    if not browse_fallback:
        return []

    print(f"[browse] {url}: no Deal API items -- using Browse discounted-item search")
    per_cat = max(max_items // len(ids), 20)
    seen: set[str] = set()
    for cid in ids:
        for it in search_items(category_id=cid, price_max=price_ceiling, max_items=per_cat,
                               env=env, credential_mode=credential_mode):
            if it.discount_pct and it.id not in seen:
                seen.add(it.id)
                items.append(it)
    return items[:max_items]


def discover_keyword(keyword: str, max_items: int, price_ceiling: float | None = None,
                     env: str = "production", credential_mode: str = "local") -> list[ApiItem]:
    """API equivalent of scraping one eBay search-results page for `keyword`."""
    return search_items(q=keyword, price_max=price_ceiling, max_items=max_items,
                        env=env, credential_mode=credential_mode)


# ---------------------------------------------------------------------------
# CLI -- manual checks against the real API before any cutover
# ---------------------------------------------------------------------------

def _print_items(items: list[ApiItem]) -> None:
    for it in items:
        disc = f" -{it.discount_pct:g}%" if it.discount_pct else ""
        print(f"{it.id}  ${it.priceNum:>7.2f}{disc:>6}  cat={it.category_id}  {it.title[:80]}")
    print(f"-- {len(items)} item(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", choices=["production", "sandbox"], default="production")
    ap.add_argument("--credential-mode", choices=["local", "bitwarden"], default="local")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token-check")
    c = sub.add_parser("categories")
    c.add_argument("--refresh", action="store_true")
    d = sub.add_parser("deals")
    d.add_argument("--category-id", action="append")
    d.add_argument("--max", type=int, default=50)
    sub.add_parser("events")
    s = sub.add_parser("search")
    s.add_argument("q")
    s.add_argument("--category-id")
    s.add_argument("--price-max", type=float)
    s.add_argument("--max", type=int, default=50)
    i = sub.add_parser("item")
    i.add_argument("legacy_id")
    u = sub.add_parser("url", help="API discovery for one DEALS_URL_POOL eBay Deals URL")
    u.add_argument("deals_url")
    u.add_argument("--max", type=int, default=100)
    u.add_argument("--price-ceiling", type=float, default=100.0)
    u.add_argument("--json", action="store_true")
    args = ap.parse_args()
    kw = {"env": args.env, "credential_mode": args.credential_mode}

    if args.cmd == "token-check":
        tok = get_application_token(args.env, args.credential_mode)
        print(f"application token OK ({len(tok)} chars)")
    elif args.cmd == "categories":
        cats = load_category_tree(refresh=args.refresh, **kw)
        print(f"{len(cats)} categories in tree (cache: {CATEGORY_CACHE_PATH})")
        for url, specs in DEALS_PAGE_CATEGORIES.items():
            print(url)
            for spec, ids in resolve_category_ids(specs, cats).items():
                paths = [" > ".join(c["path"]) for c in cats if c["id"] in ids]
                print(f"    {'OK ' if ids else 'MISSING'} {spec!r} -> {ids}")
                for p in paths:
                    print(f"          {p}")
    elif args.cmd == "deals":
        _print_items(get_deal_items(args.category_id, max_items=args.max, **kw))
    elif args.cmd == "events":
        for ev in get_events(**kw):
            print(f"{ev.get('eventId')}  {ev.get('title')}  {ev.get('startDate')} -> {ev.get('endDate')}")
    elif args.cmd == "search":
        _print_items(search_items(q=args.q, category_id=args.category_id,
                                  price_max=args.price_max, max_items=args.max, **kw))
    elif args.cmd == "item":
        data = get_item_by_legacy_id(args.legacy_id, **kw)
        print(json.dumps({k: data.get(k) for k in ("itemId", "title", "price", "marketingPrice",
                                                     "categoryPath", "itemGroupId")}, indent=2))
        print(f"estimatedSoldQuantity: {estimated_sold_quantity(data)}")
    elif args.cmd == "url":
        items = discover_deals_url(args.deals_url, args.max, args.price_ceiling, **kw)
        if args.json:
            print(json.dumps([asdict(it) for it in items], indent=2))
        else:
            _print_items(items)
    print(f"[browse] {CALL_COUNT['n']} API call(s) this run")


if __name__ == "__main__":
    main()
