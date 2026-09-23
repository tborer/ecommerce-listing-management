"""Offline tests for suppliers/cj.py (fake transport, no network)."""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse

import pytest

from ecommerce_listing_mgmt.suppliers import cj


class FakeCJ:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict, dict | None, str | None]] = []

    def __call__(self, req, timeout=None):
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path.split("/api2.0/v1/")[-1]
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, dict(urllib.parse.parse_qsl(parsed.query)), body,
                           req.headers.get("Cj-access-token")))
        handler = self.routes[path]
        status, payload = handler(body) if callable(handler) else handler
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return _Resp(raw)


class _Resp:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(data):
    return 200, {"code": 200, "result": True, "message": "Success", "data": data}


TOKEN = ok({"accessToken": "AT1", "accessTokenExpiryDate": "2099-01-01T00:00:00+08:00",
            "refreshToken": "RT1", "refreshTokenExpiryDate": "2099-06-01T00:00:00+08:00"})


def client(routes, **kw):
    fake = FakeCJ(routes)
    return cj.CJClient("CJ123@api@abc", urlopen=fake, min_interval=0, **kw), fake


def test_parsers():
    assert cj.parse_price("1.20 -- 3.40") == 1.2
    assert cj.parse_price(7) == 7.0
    assert cj.parse_price("") is None
    assert cj.parse_image('["https://a/1.jpg","https://a/2.jpg"]') == "https://a/1.jpg"
    assert cj.parse_image("https://a/1.jpg,https://a/2.jpg") == "https://a/1.jpg"
    assert cj.parse_aging("2-5") == (2, 5)
    assert cj.parse_aging("7") == (7, 7)
    assert cj.parse_aging(None) == (None, None)


def test_token_fetched_once_persisted_and_sent_as_header():
    saved = []
    c, fake = client({"authentication/getAccessToken": TOKEN,
                      "product/list": ok({"pageNum": 1, "pageSize": 20, "total": 1, "list": [
                          {"pid": "P1", "productNameEn": "Cat Tree Tower", "sellPrice": "8.50 -- 12.00",
                           "productImage": '["https://img/1.jpg"]', "productSku": "CJ-1"}]})},
                     on_tokens=saved.append)
    products = c.search_products("cat tree", warehouse_country="US")
    c.search_products("cat tree")
    assert [p.pid for p in products] == ["P1"] and products[0].price == 8.5
    assert products[0].image_url == "https://img/1.jpg"
    assert [call[1] for call in fake.calls] == ["authentication/getAccessToken", "product/list", "product/list"]
    assert fake.calls[0][3] == {"apiKey": "CJ123@api@abc"}
    assert fake.calls[1][4] == "AT1" and fake.calls[1][2]["countryCode"] == "US"
    assert "countryCode" not in fake.calls[2][2]
    assert saved and saved[0].refresh_token == "RT1"


def test_valid_stored_token_is_reused_and_expired_one_refreshed():
    c, fake = client({"product/list": ok({"list": []})},
                     tokens=cj.CJTokens("OLD", time.time() + 3600))
    c.search_products("x")
    assert [call[1] for call in fake.calls] == ["product/list"] and fake.calls[0][4] == "OLD"

    c, fake = client({"authentication/refreshAccessToken": ok({"accessToken": "NEW"}),
                      "product/list": ok({"list": []})},
                     tokens=cj.CJTokens("OLD", time.time() - 10, "RT", time.time() + 3600))
    c.search_products("x")
    assert [call[1] for call in fake.calls] == ["authentication/refreshAccessToken", "product/list"]
    assert fake.calls[1][4] == "NEW"


def test_bad_api_key_raises_clear_error():
    c, _ = client({"authentication/getAccessToken": (200, {"code": 1600001, "result": False,
                                                          "message": "Invalid API key"})})
    with pytest.raises(cj.CJError, match="Invalid API key"):
        c.ensure_token()


def test_expired_token_mid_session_retries_once_with_new_token():
    responses = iter([(200, {"code": 1600200, "result": False, "message": "token expired"}),
                      ok({"list": []})])
    c, fake = client({"authentication/getAccessToken": TOKEN,
                      "product/list": lambda body: next(responses)},
                     tokens=cj.CJTokens("STALE", time.time() + 3600))
    c.search_products("x")
    assert [call[1] for call in fake.calls] == ["product/list", "authentication/getAccessToken", "product/list"]
    assert fake.calls[-1][4] == "AT1"


def test_product_detail_variants_and_freight():
    c, fake = client({
        "authentication/getAccessToken": TOKEN,
        "product/query": ok({"pid": "P1", "productNameEn": "Cat Tree", "sellPrice": "8.5",
                             "description": "<p>Sturdy <b>sisal</b></p><p>3 levels</p>",
                             "variants": [{"vid": "V2", "variantNameEn": "Large", "variantSellPrice": 12.0},
                                          {"vid": "V1", "variantNameEn": "Small", "variantSellPrice": 8.5}]}),
        "logistic/freightCalculate": ok([
            {"logisticName": "CJPacket", "logisticPrice": 4.71, "logisticAging": "7-15"},
            {"logisticName": "USPS+", "logisticPrice": 6.10, "logisticAging": "2-5"}]),
    })
    detail = c.get_product("P1")
    assert detail.cheapest_variant().vid == "V1"
    assert detail.description == "Sturdy sisal\n3 levels"
    options = c.freight_quote("V1", "US", "CN")
    assert fake.calls[-1][3] == {"startCountryCode": "CN", "endCountryCode": "US",
                                 "products": [{"quantity": 1, "vid": "V1"}]}
    assert [o.name for o in options] == ["CJPacket", "USPS+"]
    assert cj.cheapest_option_within(options, 10).name == "USPS+"
    assert cj.cheapest_option_within(options, 20).name == "CJPacket"
    assert cj.cheapest_option_within(options, 3) is None


def test_network_failure_becomes_cj_error():
    def down(req, timeout=None):
        raise urllib.error.URLError("Tunnel connection failed: 403")

    c = cj.CJClient("CJ123@api@abc", urlopen=down, min_interval=0)
    with pytest.raises(cj.CJError, match="couldn't reach CJdropshipping"):
        c.ensure_token()
