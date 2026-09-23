"""Offline tests for ebay/browse.py and the pipeline's API discovery path.
No network, no credentials: the HTTP transport and app token are faked."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from ecommerce_listing_mgmt import pipeline
from ecommerce_listing_mgmt.ebay import browse


class FakeEbay:
    """Routes requests by path to canned responses; records every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, req):
        parsed = urllib.parse.urlparse(req.full_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.requests.append((parsed.path, params))
        handler = self.routes.get(parsed.path)
        if handler is None:
            raise AssertionError(f"unexpected request {parsed.path}")
        status, body = handler(params) if callable(handler) else handler
        raw = json.dumps(body).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return _Resp(raw)


class _Resp:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _item(legacy, title, price, discount=None, cat="1281"):
    d = {"itemId": f"v1|{legacy}|0", "legacyItemId": legacy, "title": title,
         "price": {"value": str(price), "currency": "USD"}, "categoryId": cat,
         "image": {"imageUrl": f"https://i.ebayimg.com/{legacy}.jpg"}}
    if discount is not None:
        d["marketingPrice"] = {"discountPercentage": str(discount),
                               "originalPrice": {"value": str(round(price * 1.5, 2)), "currency": "USD"}}
    return d


TREE = {"rootCategoryNode": {"category": {"categoryId": "0", "categoryName": "Root"},
    "childCategoryTreeNodes": [
        {"category": {"categoryId": "1281", "categoryName": "Pet Supplies"}},
        {"category": {"categoryId": "11700", "categoryName": "Home & Garden"},
         "childCategoryTreeNodes": [
             {"category": {"categoryId": "631", "categoryName": "Tools & Workshop Equipment"}}]},
        {"category": {"categoryId": "12576", "categoryName": "Business & Industrial"},
         "childCategoryTreeNodes": [
             {"category": {"categoryId": "999", "categoryName": "Tools & Workshop Equipment"}}]},
        {"category": {"categoryId": "1059", "categoryName": "Men's Accessories"},
         "childCategoryTreeNodes": [{"category": {"categoryId": "79720", "categoryName": "Sunglasses"}}]},
        {"category": {"categoryId": "4251", "categoryName": "Women's Accessories"},
         "childCategoryTreeNodes": [{"category": {"categoryId": "45246", "categoryName": "Sunglasses"}}]},
    ]}}


@pytest.fixture(autouse=True)
def _no_real_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(browse, "get_application_token", lambda env, mode: "TEST-TOKEN")
    monkeypatch.setattr(browse, "CATEGORY_CACHE_PATH", tmp_path / "tree.json")
    browse.CALL_COUNT["n"] = 0


def _install(monkeypatch, routes) -> FakeEbay:
    fake = FakeEbay(routes)
    monkeypatch.setattr(browse, "_urlopen", fake)
    return fake


TREE_ROUTES = {
    "/commerce/taxonomy/v1/get_default_category_tree_id": (200, {"categoryTreeId": "0"}),
    "/commerce/taxonomy/v1/category_tree/0": (200, TREE),
}


def test_parse_item_normalizes_to_scraper_tile_shape():
    it = browse.parse_item(_item("395766041202", "Dog Playpen", 42.5, discount=30), "deal")
    assert it.to_tile() == {"id": "395766041202", "title": "Dog Playpen", "priceNum": 42.5,
                            "url": "https://www.ebay.com/itm/395766041202"}
    assert it.discount_pct == 30.0 and it.original_price == 63.75
    # Must be directly usable by the pipeline's candidate type.
    pipeline.EbayCandidate(**it.to_tile())


def test_parse_item_falls_back_to_restful_item_id_and_drops_unusable():
    raw = _item("123", "X", 5)
    del raw["legacyItemId"]
    assert browse.parse_item(raw, "browse").id == "123"
    assert browse.parse_item({**_item("1", "X", 5), "price": None}, "browse") is None
    assert browse.parse_item({**_item("1", "X", 5), "price": {"value": "5", "currency": "GBP"}}, "browse") is None
    assert browse.parse_item({**_item("1", "", 5)}, "browse") is None
    assert browse.parse_item({**_item("1", "X", 5), "primaryItemGroup": None}, "browse") is not None


def test_build_browse_filter():
    assert browse.build_browse_filter(100.0) == "price:[..100],priceCurrency:USD,buyingOptions:{FIXED_PRICE}"
    assert browse.build_browse_filter(None, fixed_price_only=False) is None
    assert browse.build_browse_filter(None, conditions=["NEW"]) == "buyingOptions:{FIXED_PRICE},conditions:{NEW}"


def test_deal_items_paginate_until_no_next(monkeypatch):
    def deals(params):
        offset = int(params["offset"])
        if offset == 0:
            return 200, {"dealItems": [_item(str(i), f"t{i}", 10) for i in range(3)],
                         "next": "x", "total": 5}
        return 200, {"dealItems": [_item(str(i), f"t{i}", 10) for i in range(3, 5)], "total": 5}

    fake = _install(monkeypatch, {"/buy/deal/v1/deal_item": deals})
    items = browse.get_deal_items(["1281", "631"], max_items=50)
    assert [i.id for i in items] == ["0", "1", "2", "3", "4"]
    assert fake.requests[0][1]["category_ids"] == "1281,631"
    assert [r[1]["offset"] for r in fake.requests] == ["0", "3"]


def test_max_items_caps_page_size_and_total(monkeypatch):
    fake = _install(monkeypatch, {"/buy/browse/v1/item_summary/search":
                                  (200, {"itemSummaries": [_item(str(i), "t", 1) for i in range(7)], "next": "x"})})
    items = browse.search_items(q="mouse pad", price_max=100, max_items=7)
    assert len(items) == 7 and len(fake.requests) == 1
    params = fake.requests[0][1]
    assert params["limit"] == "7" and params["q"] == "mouse pad"
    assert params["filter"].startswith("price:[..100]")


def test_search_requires_query_or_category():
    with pytest.raises(ValueError):
        browse.search_items()


def test_api_error_carries_ebay_message(monkeypatch):
    _install(monkeypatch, {"/buy/deal/v1/deal_item":
                           (403, {"errors": [{"errorId": 1100, "longMessage": "Insufficient permissions"}]})})
    with pytest.raises(browse.EbayApiError, match="1100: Insufficient permissions"):
        browse.get_deal_items(["1281"])


def test_resolve_category_ids_by_name_and_path_suffix(monkeypatch):
    _install(monkeypatch, TREE_ROUTES)
    cats = browse.load_category_tree()
    got = browse.resolve_category_ids(
        ["pet supplies", "Sunglasses", "Home & Garden > Tools & Workshop Equipment", "Nope"], cats)
    assert got == {"pet supplies": ["1281"], "Sunglasses": ["79720", "45246"],
                   "Home & Garden > Tools & Workshop Equipment": ["631"], "Nope": []}


def test_category_tree_is_cached_on_disk(monkeypatch):
    fake = _install(monkeypatch, TREE_ROUTES)
    browse.load_category_tree()
    browse.load_category_tree()
    assert len(fake.requests) == 2  # tree-id + tree, once


def test_discover_deals_url_uses_deal_api_and_price_ceiling(monkeypatch):
    url = "https://www.ebay.com/deals/home-garden/pet-supplies"
    fake = _install(monkeypatch, {**TREE_ROUTES, "/buy/deal/v1/deal_item":
                                  (200, {"dealItems": [_item("1", "cheap", 20), _item("2", "pricey", 150)]})})
    items = browse.discover_deals_url(url, max_items=100, price_ceiling=100)
    assert [i.id for i in items] == ["1"]
    assert fake.requests[-1][1]["category_ids"] == "1281"


def test_discover_deals_url_falls_back_to_discounted_browse(monkeypatch):
    url = "https://www.ebay.com/deals/home-garden/pet-supplies"
    fake = _install(monkeypatch, {
        **TREE_ROUTES,
        "/buy/deal/v1/deal_item": (403, {"errors": [{"errorId": 1100, "message": "no access"}]}),
        "/buy/browse/v1/item_summary/search": (200, {"itemSummaries": [
            _item("10", "discounted", 30, discount=25), _item("11", "full price", 30)]}),
    })
    items = browse.discover_deals_url(url, max_items=100, price_ceiling=100)
    assert [i.id for i in items] == ["10"]
    search_params = fake.requests[-1][1]
    assert search_params["category_ids"] == "1281" and "price:[..100]" in search_params["filter"]


def test_discover_deals_url_unmapped_and_unresolved(monkeypatch):
    _install(monkeypatch, TREE_ROUTES)
    with pytest.raises(KeyError):
        browse.discover_deals_url("https://www.ebay.com/deals/unknown", 10)
    # A mapped URL whose names aren't in this (tiny) test tree resolves to nothing.
    assert browse.discover_deals_url("https://www.ebay.com/deals/trending/home-garden/crafts", 10) == []


def test_every_ebay_deals_pool_url_has_an_api_mapping():
    ebay_urls = [u for u in pipeline.DEALS_URL_POOL if u.startswith("https://www.ebay.com/deals/")]
    assert ebay_urls and all(u in browse.DEALS_PAGE_CATEGORIES for u in ebay_urls)


# --- pipeline wiring ---------------------------------------------------------

def test_pipeline_api_source_does_not_touch_browser_for_deals_urls(monkeypatch):
    url = "https://www.ebay.com/deals/home-garden/pet-supplies"
    monkeypatch.setattr(pipeline, "browser_navigate", lambda *a, **k: pytest.fail("browser used"))
    monkeypatch.setattr(browse, "discover_deals_url",
                        lambda u, n, ceiling: [browse.parse_item(_item("7", "Cat Tree", 33), "deal")])
    assert pipeline._scrape_tiles_for_url(url, "api", 100.0) == [
        {"id": "7", "title": "Cat Tree", "priceNum": 33.0, "url": "https://www.ebay.com/itm/7"}]
    assert not pipeline._needs_browser([url], "api")
    assert pipeline._needs_browser([url], "scrape")


def test_pipeline_api_source_keyword_pages_extract_then_search_api(monkeypatch):
    url = next(iter(pipeline.KEYWORD_SOURCES))
    monkeypatch.setattr(pipeline, "browser_navigate", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "browser_evaluate", lambda js: ["LED strip", "Neck fan"])
    searched = []

    def fake_kw(kw, n, ceiling):
        searched.append((kw, n, ceiling))
        return [browse.parse_item(_item(str(len(searched)), kw, 12), "browse")]

    monkeypatch.setattr(browse, "discover_keyword", fake_kw)
    tiles = pipeline._scrape_tiles_for_url(url, "api", 100.0)
    assert [t["title"] for t in tiles] == ["LED strip", "Neck fan"]
    assert searched == [("LED strip", 50, 100.0), ("Neck fan", 50, 100.0)]
    assert pipeline._needs_browser([url], "api")


def test_pipeline_api_error_on_one_url_yields_empty_not_crash(monkeypatch):
    url = "https://www.ebay.com/deals/home-garden/pet-supplies"

    def boom(*a, **k):
        raise browse.EbayApiError(500, "/buy/deal/v1/deal_item", {"errors": []})

    monkeypatch.setattr(browse, "discover_deals_url", boom)
    assert pipeline._scrape_tiles_for_url(url, "api", 100.0) == []


def test_scrape_ebay_tiles_applies_ceiling_exclusions_and_dedup_to_api_tiles(monkeypatch):
    tiles = [
        {"id": "1", "title": "Cat Tree", "priceNum": 33.0, "url": "u1"},
        {"id": "2", "title": "Anti-snoring mouth tape", "priceNum": 9.0, "url": "u2"},
        {"id": "3", "title": "Big Desk", "priceNum": 150.0, "url": "u3"},
        {"id": "1", "title": "Cat Tree", "priceNum": 33.0, "url": "u1"},
    ]
    monkeypatch.setattr(pipeline, "_scrape_tiles_for_url", lambda url, src, ceiling: tiles)
    seen: set[str] = set()
    got = pipeline._scrape_ebay_tiles(["a"], 100.0, seen, None, "api")
    assert [c.id for c in got] == ["1"]
