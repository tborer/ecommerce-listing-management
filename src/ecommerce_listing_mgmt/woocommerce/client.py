#!/usr/bin/env python3
"""Branch 11: WooCommerce REST API client for the DSers-bridge store.

This store (`branch11-wordpress`/`branch11-mysql` Docker containers, built
2026-08-28/29, public at branch11_woocommerce_creds.json's `site_url_public`
via Tailscale Funnel) exists purely as a technical bridge so DSers -- which
has no native eBay integration -- can ingest Branch 11's eBay orders. No real
customer ever browses it.

Auth: WooCommerce's REST API only accepts simple consumer_key/consumer_secret
query-string auth over HTTPS (`is_ssl()` true) -- over plain HTTP it demands
full OAuth 1.0a request signing instead (confirmed 2026-08-29 by reading
class-wc-rest-authentication.php directly, not guessed). Always use
`site_url_public`, never `site_url_local`, for anything beyond a same-host
smoke test.

Credentials: `branch11_woocommerce_creds.json` (mode 600), same pattern as
the eBay/AliExpress local-creds file.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CREDS_PATH = Path(__file__).parent / "branch11_woocommerce_creds.json"


def load_credentials() -> dict:
    if not CREDS_PATH.exists():
        raise RuntimeError(f"{CREDS_PATH} does not exist -- run the WooCommerce setup first.")
    return json.loads(CREDS_PATH.read_text())


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None,
          use_public_url: bool = True) -> tuple[int, dict | list | None]:
    creds = load_credentials()
    base = creds["site_url_public"] if use_public_url else creds["site_url_local"]
    query = dict(params or {})
    query["consumer_key"] = creds["wc_rest_consumer_key"]
    query["consumer_secret"] = creds["wc_rest_consumer_secret"]
    url = f"{base}/wp-json/wc/v3{path}?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")}


def list_products(per_page: int = 20) -> tuple[int, dict | list | None]:
    return _call("GET", "/products", {"per_page": per_page})


def get_product(product_id: int) -> tuple[int, dict | list | None]:
    return _call("GET", f"/products/{product_id}")


def create_product(name: str, sku: str, price: str, description: str = "") -> tuple[int, dict | list | None]:
    """`sku` should match the Branch 11 `branch11-<ebay_item_id>` convention so
    an incoming order line item's SKU can be traced back to this product and,
    once DSers is connected, to its mapped AliExpress source -- see
    branch11-listing-rules.md's DSers hybrid architecture section."""
    body = {"name": name, "sku": sku, "regular_price": str(price),
             "description": description, "type": "simple"}
    return _call("POST", "/products", body=body)


def delete_product(product_id: int, force: bool = True) -> tuple[int, dict | list | None]:
    return _call("DELETE", f"/products/{product_id}", {"force": str(force).lower()})


def list_orders(per_page: int = 20) -> tuple[int, dict | list | None]:
    return _call("GET", "/orders", {"per_page": per_page})


def get_order(order_id: int) -> tuple[int, dict | list | None]:
    return _call("GET", f"/orders/{order_id}")


def update_order(order_id: int, body: dict) -> tuple[int, dict | list | None]:
    return _call("PUT", f"/orders/{order_id}", body=body)


def create_order(line_items: list[dict], status: str = "pending",
                  billing: dict | None = None, shipping: dict | None = None) -> tuple[int, dict | list | None]:
    """`line_items`: list of {"product_id": int, "quantity": int} (or
    {"sku": str, "quantity": int} -- WooCommerce resolves either). `status`
    defaults to "pending", not "processing" -- DSers's own sync is documented
    to pick up orders regardless of status, and "pending" avoids implying a
    real payment was captured on this bridge store (none ever is; real
    payment only ever happens on AliExpress, per Branch 11's Hard
    Constraints).

    **`shipping` must be passed explicitly and separately from `billing` --
    confirmed live 2026-08-29 that WooCommerce does NOT copy billing into
    shipping automatically via the API** (that's checkout-UI-only behavior,
    a "same as billing" checkbox, not a server-side default). A real caller
    (Task 4: pushing an eBay order's buyer address in) MUST populate
    `shipping` with the real eBay ship-to address or DSers will show the
    order with no shipping destination at all -- confirmed live via DSers's
    own dashboard, not just inferred."""
    body: dict = {"status": status, "line_items": line_items}
    if billing:
        body["billing"] = billing
    if shipping:
        body["shipping"] = shipping
    return _call("POST", "/orders", body=body)


def delete_order(order_id: int, force: bool = True) -> tuple[int, dict | list | None]:
    return _call("DELETE", f"/orders/{order_id}", {"force": str(force).lower()})


def system_status() -> tuple[int, dict | list | None]:
    return _call("GET", "/system_status")
