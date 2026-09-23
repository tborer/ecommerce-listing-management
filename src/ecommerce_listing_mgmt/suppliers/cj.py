"""CJdropshipping API 2.0 client -- product search, product detail/variants,
and freight quotes (web-app-plan.md section 4a, step 4). Orders come later.

Auth: the user's CJ API key (CJ dashboard > Authorization > API) is exchanged
for an access token via authentication/getAccessToken; calls then send it as
the `CJ-Access-Token` header. CJ caches tokens server-side (same token back
for ~24h) and rate-limits the token endpoint, so callers should persist the
tokens this client hands to `on_tokens` and pass them back in next time.

Response envelope: {"code": 200, "result": true, "message": ..., "data": ...}.
Field names follow CJ's published examples; the ones CJ documents loosely
(sellPrice as a range string, productImage as a JSON-encoded list, date
formats) are parsed defensively. Endpoints and throttling defaults should be
re-checked against https://developers.cjdropshipping.com when first run live.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

# CJ throttles per account (free tier is low); keep calls spaced out.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1
DEFAULT_TOKEN_LIFETIME_SECONDS = 14 * 24 * 3600


class CJError(RuntimeError):
    def __init__(self, message: str, code: object = None, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(f"CJ API error{f' {code}' if code is not None else ''}: {message}")


@dataclass
class CJTokens:
    access_token: str
    access_expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None

    def access_valid(self, now: float | None = None) -> bool:
        return bool(self.access_token) and self.access_expires_at - 300 > (now or time.time())

    def refresh_valid(self, now: float | None = None) -> bool:
        return bool(self.refresh_token) and (self.refresh_expires_at or 0) > (now or time.time())

    def to_dict(self) -> dict:
        return {"access_token": self.access_token, "access_expires_at": self.access_expires_at,
                "refresh_token": self.refresh_token, "refresh_expires_at": self.refresh_expires_at}

    @classmethod
    def from_dict(cls, d: dict | None) -> CJTokens | None:
        if not d or not d.get("access_token"):
            return None
        return cls(d["access_token"], float(d.get("access_expires_at") or 0),
                   d.get("refresh_token"), d.get("refresh_expires_at"))


@dataclass
class CJProduct:
    pid: str
    title: str
    price: float | None          # lowest sell price across variants, USD
    image_url: str | None
    sku: str | None = None
    category: str | None = None

    @property
    def url(self) -> str:
        return f"https://cjdropshipping.com/product/-p-{self.pid}.html"


@dataclass
class CJVariant:
    vid: str
    name: str
    price: float | None
    sku: str | None = None
    image_url: str | None = None


@dataclass
class CJProductDetail(CJProduct):
    description: str = ""
    weight_grams: float | None = None
    variants: list[CJVariant] = field(default_factory=list)

    def cheapest_variant(self) -> CJVariant | None:
        priced = [v for v in self.variants if v.price is not None]
        return min(priced, key=lambda v: v.price) if priced else (self.variants[0] if self.variants else None)


@dataclass
class FreightOption:
    name: str
    price: float
    min_days: int | None
    max_days: int | None


def parse_price(value: object) -> float | None:
    """CJ prices arrive as numbers, "12.5", or ranges like "1.20 -- 3.40".
    Returns the lowest number found."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]
    return min(nums) if nums else None


def parse_image(value: object) -> str | None:
    """productImage is a URL, a list, or a JSON-encoded list of URLs."""
    if not value:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    text = str(value).strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
            return str(items[0]) if items else None
        except json.JSONDecodeError:
            pass
    return text.split(",")[0].strip() or None


def parse_aging(value: object) -> tuple[int | None, int | None]:
    """logisticAging like "2-5" (days) -> (2, 5); "7" -> (7, 7)."""
    nums = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _parse_expiry(value: object, fallback_seconds: float) -> float:
    if isinstance(value, (int, float)):
        # epoch seconds or milliseconds
        return float(value) / (1000 if value > 1e12 else 1)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + fallback_seconds


def _strip_html(html: str) -> str:
    text = re.sub(r"<(br|/p|/div|/li)\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


class CJClient:
    def __init__(self, api_key: str, tokens: CJTokens | None = None,
                 on_tokens: Callable[[CJTokens], None] | None = None,
                 min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
                 urlopen: Callable = urllib.request.urlopen, base_url: str = BASE_URL,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise ValueError("CJ API key is required")
        self.api_key = api_key
        self.tokens = tokens
        self.on_tokens = on_tokens
        self.min_interval = min_interval
        self._urlopen = urlopen
        self._sleep = sleep
        self.base_url = base_url.rstrip("/")
        self._last_call = 0.0
        self.call_count = 0

    # -- transport ---------------------------------------------------------

    def _raw(self, method: str, path: str, params: dict | None = None,
             body: dict | None = None, token: str | None = None) -> dict:
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["CJ-Access-Token"] = token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        self._last_call = time.monotonic()
        self.call_count += 1
        try:
            with self._urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise CJError(raw.decode(errors="replace")[:300], status=e.code) from None
            payload.setdefault("_http_status", e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CJError(f"couldn't reach CJdropshipping ({getattr(e, 'reason', e)})") from None
        return payload

    @staticmethod
    def _ok(payload: dict) -> bool:
        return payload.get("result") is True or payload.get("code") == 200 or payload.get("success") is True

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        token = self.ensure_token()
        payload = self._raw(method, path, params, body, token)
        if not self._ok(payload) and self._looks_like_auth_failure(payload):
            self.tokens = None
            payload = self._raw(method, path, params, body, self.ensure_token())
        if not self._ok(payload):
            raise CJError(payload.get("message") or "request failed", payload.get("code"),
                          payload.get("_http_status"))
        return payload.get("data")

    @staticmethod
    def _looks_like_auth_failure(payload: dict) -> bool:
        msg = str(payload.get("message") or "").lower()
        return payload.get("_http_status") == 401 or ("token" in msg and ("expire" in msg or "invalid" in msg))

    # -- auth --------------------------------------------------------------

    def _store(self, data: dict) -> CJTokens:
        if not data or not data.get("accessToken"):
            raise CJError("token response had no accessToken")
        self.tokens = CJTokens(
            access_token=data["accessToken"],
            access_expires_at=_parse_expiry(data.get("accessTokenExpiryDate"), DEFAULT_TOKEN_LIFETIME_SECONDS),
            refresh_token=data.get("refreshToken"),
            refresh_expires_at=_parse_expiry(data.get("refreshTokenExpiryDate"), 180 * 24 * 3600)
            if data.get("refreshToken") else None,
        )
        if self.on_tokens:
            self.on_tokens(self.tokens)
        return self.tokens

    def ensure_token(self) -> str:
        if self.tokens and self.tokens.access_valid():
            return self.tokens.access_token
        if self.tokens and self.tokens.refresh_valid():
            payload = self._raw("POST", "authentication/refreshAccessToken",
                                body={"refreshToken": self.tokens.refresh_token})
            if self._ok(payload):
                return self._store(payload.get("data") or {}).access_token
        payload = self._raw("POST", "authentication/getAccessToken", body={"apiKey": self.api_key})
        if not self._ok(payload):
            raise CJError(payload.get("message") or "could not get an access token -- check the API key",
                          payload.get("code"), payload.get("_http_status"))
        return self._store(payload.get("data") or {}).access_token

    # -- products ----------------------------------------------------------

    def search_products(self, keyword: str, page_size: int = 20,
                        warehouse_country: str | None = None) -> list[CJProduct]:
        data = self._call("GET", "product/list", params={
            "productNameEn": keyword, "pageNum": 1, "pageSize": page_size,
            "countryCode": warehouse_country})
        out = []
        for raw in (data or {}).get("list") or []:
            if not raw.get("pid") or not raw.get("productNameEn"):
                continue
            out.append(CJProduct(
                pid=str(raw["pid"]), title=str(raw["productNameEn"]).strip(),
                price=parse_price(raw.get("sellPrice")), image_url=parse_image(raw.get("productImage")),
                sku=raw.get("productSku"), category=raw.get("categoryName")))
        return out

    def get_product(self, pid: str) -> CJProductDetail:
        raw = self._call("GET", "product/query", params={"pid": pid}) or {}
        variants = [
            CJVariant(vid=str(v["vid"]), name=str(v.get("variantNameEn") or v.get("variantKey") or ""),
                      price=parse_price(v.get("variantSellPrice")), sku=v.get("variantSku"),
                      image_url=parse_image(v.get("variantImage")))
            for v in raw.get("variants") or [] if v.get("vid")]
        return CJProductDetail(
            pid=str(raw.get("pid") or pid), title=str(raw.get("productNameEn") or "").strip(),
            price=parse_price(raw.get("sellPrice")), image_url=parse_image(raw.get("productImage")),
            sku=raw.get("productSku"), category=raw.get("categoryName"),
            description=_strip_html(raw.get("description") or ""),
            weight_grams=parse_price(raw.get("productWeight")), variants=variants)

    def freight_quote(self, vid: str, end_country: str = "US", start_country: str = "CN",
                      quantity: int = 1) -> list[FreightOption]:
        data = self._call("POST", "logistic/freightCalculate", body={
            "startCountryCode": start_country, "endCountryCode": end_country,
            "products": [{"quantity": quantity, "vid": vid}]})
        options = []
        for raw in data or []:
            price = parse_price(raw.get("logisticPrice"))
            if price is None or not raw.get("logisticName"):
                continue
            lo, hi = parse_aging(raw.get("logisticAging"))
            options.append(FreightOption(str(raw["logisticName"]), price, lo, hi))
        return sorted(options, key=lambda o: o.price)


def cheapest_option_within(options: list[FreightOption], max_days: int | None) -> FreightOption | None:
    """Cheapest option whose worst-case delivery fits `max_days` (unknown
    delivery time never qualifies when a limit is set)."""
    for opt in sorted(options, key=lambda o: o.price):
        if max_days is None or (opt.max_days is not None and opt.max_days <= max_days):
            return opt
    return None
