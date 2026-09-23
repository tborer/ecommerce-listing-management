#!/usr/bin/env python3
"""Branch 11 Step 8: eBay Fulfillment API -- pull open orders and prep them
for Step 9 (AliExpress supplier ordering).

Read-only against eBay: the only eBay API call this module makes is
GET /sell/fulfillment/v1/order. Uses sell.fulfillment.readonly, already
granted on both sandbox and production (see branch11-listing-rules.md's
"eBay OAuth" section, and the 2026-08-26 sandbox test confirming getOrders
returns real data) -- no new consent needed.

**Added 2026-08-31 (PRODUCTION-READINESS.md Item 30, Travis-directed): also
mirrors each fully-sourced real eBay order into the DSers-bridge WooCommerce
store** (`branch11_woocommerce.create_order()`), so DSers's own bulk "Place
Order" flow -- and its native tracking-auto-sync once the supplier ships --
has a real order object to act on. Researched live 2026-08-31: neither the
DSers MCP tool catalog nor DSers's Partner/Open API (wrong audience -- that
program is for third-party app builders, not a single merchant automating
their own account) exposes order placement; DSers's own native bulk-order
feature still requires a human to manually complete payment on AliExpress's
checkout page even from inside DSers's UI -- consistent with, not a
workaround for, this project's Hard Constraint that a real supplier order
can never be placed autonomously. This addition closes the actual missing
link found while researching that: DSers's auto-order/auto-tracking-sync
features only work against a real order in the store DSers is connected to,
and nothing previously created one. **Still never places a supplier order,
never touches eBay order/tracking state, and never emails/messages a
customer** -- Steps 9-11 (Travis's own DSers approval click, tracking
capture, marking the eBay order shipped) stay fully manual/deferred, this
only makes the order visible to Travis inside DSers instead of only inside
an email.

Cross-references each order line item's SKU against branch11_auto_listed.json
(the Step 7 listing ledger) to recover the AliExpress source URL/price needed
to actually place the supplier order. The ledger write was extended
2026-08-28 to persist this going forward; entries from before that fix (or
any listing made outside the auto-listing path, e.g. a fully manual
chat-approved SKU never routed through run_auto_listing()) may be missing it
-- those get flagged needs_manual_sourcing_lookup rather than guessed, since
a wrong AliExpress URL here means sourcing the wrong physical product.

"Pending" = orderFulfillmentStatus in {NOT_STARTED, IN_PROGRESS}. No separate
dedup ledger: eBay's own fulfillment status is the source of truth for
done/not-done, so an order simply stops appearing here once Travis (outside
this system, via eBay Seller Hub) marks it fulfilled with tracking -- no
extra state to keep in sync.

Usage:
    python3 branch11_orders.py <sandbox|production> [--credential-mode bitwarden|local]

Output: writes branch11_orders_pending_<date>.json in this directory --
does not overwrite prior days' files, matching branch11_candidates_<date>.json's
own pattern.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from ecommerce_listing_mgmt.ebay.auth import ENVIRONMENTS
from ecommerce_listing_mgmt.ebay.listing import _call, _error_detail

OUT_DIR = Path(__file__).parent
AUTO_LISTED_LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"

PENDING_FULFILLMENT_STATUSES = ("NOT_STARTED", "IN_PROGRESS")


def load_listing_ledger() -> dict:
    if AUTO_LISTED_LEDGER_PATH.exists():
        return json.loads(AUTO_LISTED_LEDGER_PATH.read_text())
    return {}


def fetch_orders(env: str, credential_mode: str = "bitwarden") -> list[dict]:
    """Pages through GET /sell/fulfillment/v1/order for orders whose
    fulfillment status is NOT_STARTED or IN_PROGRESS. Raises on a non-200 --
    a broken order feed shouldn't fail silently into "no orders today"."""
    orders: list[dict] = []
    filter_val = "orderfulfillmentstatus:{" + "|".join(PENDING_FULFILLMENT_STATUSES) + "}"
    offset = 0
    limit = 50
    while True:
        path = (f"/sell/fulfillment/v1/order"
                f"?filter={filter_val}&limit={limit}&offset={offset}")
        status, body = _call(env, "GET", path, credential_mode=credential_mode)
        if status != 200:
            raise RuntimeError(f"getOrders failed: {_error_detail(status, body)}")
        batch = body.get("orders", [])
        orders.extend(batch)
        total = body.get("total", len(orders))
        offset += limit
        if offset >= total or not batch:
            break
    return orders


def _extract_ship_to(order: dict) -> dict:
    """Pulls the eBay buyer's own shipping address -- Step 9 must ship the
    supplier order here, never to Travis's own address (see
    branch11-listing-rules.md's "Step 9 mechanics findings")."""
    instructions = order.get("fulfillmentStartInstructions") or []
    ship_to = (instructions[0].get("shippingStep", {}).get("shipTo")
               if instructions else None) or {}
    addr = ship_to.get("contactAddress") or {}
    return {
        "full_name": ship_to.get("fullName"),
        "address_line1": addr.get("addressLine1"),
        "address_line2": addr.get("addressLine2"),
        "city": addr.get("city"),
        "state": addr.get("stateOrProvince"),
        "postal_code": addr.get("postalCode"),
        "country": addr.get("countryCode"),
        "phone": (ship_to.get("primaryPhone") or {}).get("phoneNumber"),
    }


def _prep_line_item(item: dict, ledger: dict) -> dict:
    sku = item.get("sku")
    ledger_entry = ledger.get(sku) if sku else None
    if ledger_entry and ledger_entry.get("ali_url"):
        sourcing = {
            "status": "FOUND",
            "ali_url": ledger_entry["ali_url"],
            "ali_price": ledger_entry.get("ali_price"),
            "ali_shipping_cost": ledger_entry.get("ali_shipping_cost"),
            # Added 2026-08-31 for the WooCommerce order-push below -- a line
            # item can only be pushed if its product actually exists in the
            # DSers-bridge store (set by _mirror_to_woocommerce() at listing
            # time; None if that mirror failed or predates the mirror step).
            "woo_product_id": ledger_entry.get("woo_product_id"),
        }
    elif ledger_entry:
        sourcing = {"status": "NEEDS_MANUAL_SOURCING_LOOKUP",
                     "reason": "sku is in the listing ledger but it predates "
                               "the 2026-08-28 fix that records the AliExpress "
                               "source URL there -- look it up by hand"}
    else:
        sourcing = {"status": "NEEDS_MANUAL_SOURCING_LOOKUP",
                     "reason": "sku not found in branch11_auto_listed.json -- "
                               "likely listed manually outside the auto-listing path"}
    cost = item.get("lineItemCost") or {}
    return {
        "sku": sku,
        "title": item.get("title"),
        "quantity": item.get("quantity"),
        "line_item_cost": cost.get("value"),
        "currency": cost.get("currency"),
        "sourcing": sourcing,
    }


def prep_orders(orders: list[dict], ledger: dict) -> list[dict]:
    prepped = []
    for order in orders:
        prepped.append({
            "order_id": order.get("orderId"),
            "creation_date": order.get("creationDate"),
            "order_fulfillment_status": order.get("orderFulfillmentStatus"),
            "ship_to": _extract_ship_to(order),
            "line_items": [_prep_line_item(li, ledger) for li in order.get("lineItems", [])],
        })
    return prepped


WOO_PUSH_LEDGER_PATH = OUT_DIR / "branch11_orders_pushed_to_woo.json"


def load_woo_push_ledger() -> dict:
    if WOO_PUSH_LEDGER_PATH.exists():
        return json.loads(WOO_PUSH_LEDGER_PATH.read_text())
    return {}


def _save_woo_push_ledger(ledger: dict) -> None:
    WOO_PUSH_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def _woo_shipping_from_ship_to(ship_to: dict) -> dict:
    """WooCommerce's `shipping` address shape from eBay's ship_to fields --
    confirmed live 2026-08-29 (see branch11_woocommerce.create_order()'s
    docstring) that WooCommerce does NOT auto-copy billing into shipping via
    the API, so this must be populated explicitly and directly."""
    full_name = (ship_to.get("full_name") or "").strip()
    first, _, last = full_name.partition(" ")
    return {
        "first_name": first,
        "last_name": last,
        "address_1": ship_to.get("address_line1") or "",
        "address_2": ship_to.get("address_line2") or "",
        "city": ship_to.get("city") or "",
        "state": ship_to.get("state") or "",
        "postcode": ship_to.get("postal_code") or "",
        "country": ship_to.get("country") or "",
        "phone": ship_to.get("phone") or "",
    }


def push_order_to_woocommerce(prepped_order: dict, push_ledger: dict) -> dict:
    """Creates a matching order in the DSers-bridge WooCommerce store for one
    real eBay order, so DSers's own bulk 'Place Order' flow -- and its
    native tracking-auto-sync once the supplier ships -- has a real order
    object to act on (see this module's docstring / PRODUCTION-READINESS.md
    Item 30). Never places anything on AliExpress and never moves money --
    this order sits at WooCommerce status "pending", the same staging-only
    role `_mirror_to_woocommerce()` already plays for products.

    **Idempotent via `push_ledger`** (keyed by eBay order_id, persisted to
    WOO_PUSH_LEDGER_PATH): an already-pushed order is never re-pushed. This
    is necessary because eBay's own fulfillment status doesn't change just
    because this script ran again -- an order stays NOT_STARTED/IN_PROGRESS
    in eBay's system until Travis fulfills it there (per this module's
    original no-separate-dedup-ledger design) -- so without this separate
    ledger, every run would create a duplicate WooCommerce order for the
    same real sale.

    **Only pushes if every line item is fully sourced AND mirrored** (has a
    `woo_product_id` -- the WooCommerce product must already exist for
    WooCommerce to resolve an order line item by SKU). A partial push
    (skipping just the un-sourced line items) would create an order in
    WooCommerce/DSers that's missing part of what the real buyer actually
    ordered -- worse than not pushing at all, same "wrong data is worse
    than no data" principle as everywhere else in this pipeline. Deliberately
    fails closed and never raises -- a WooCommerce hiccup must never break
    the rest of this script's (already-working) order reporting."""
    order_id = prepped_order["order_id"]
    if order_id in push_ledger:
        return {"status": "ALREADY_PUSHED", "woo_order_id": push_ledger[order_id].get("woo_order_id")}

    line_items = prepped_order["line_items"]
    not_ready = [
        li for li in line_items
        if li["sourcing"]["status"] != "FOUND" or not li["sourcing"].get("woo_product_id")
    ]
    if not line_items or not_ready:
        return {
            "status": "SKIPPED_MISSING_SOURCING",
            "reason": f"{len(not_ready)}/{len(line_items)} line item(s) not fully sourced/mirrored yet",
        }

    try:
        from ecommerce_listing_mgmt.woocommerce import client as woo
        woo_line_items = [{"sku": li["sku"], "quantity": li["quantity"]} for li in line_items]
        shipping = _woo_shipping_from_ship_to(prepped_order["ship_to"])
        status, body = woo.create_order(line_items=woo_line_items, status="pending", shipping=shipping)
    except Exception as e:  # noqa: BLE001 -- fail closed, never break the rest of the run
        return {"status": "PUSH_ERROR", "reason": str(e)}

    if status != 201 or not isinstance(body, dict) or not body.get("id"):
        return {"status": "PUSH_ERROR", "reason": f"HTTP {status}: {body}"}

    push_ledger[order_id] = {
        "woo_order_id": body["id"],
        "pushed_at": date.today().isoformat(),
        "line_item_skus": [li["sku"] for li in line_items],
    }
    _save_woo_push_ledger(push_ledger)  # persist immediately, not just at the end -- a mid-run crash must not lose track of a real push that already happened
    return {"status": "PUSHED", "woo_order_id": body["id"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("env", choices=ENVIRONMENTS)
    parser.add_argument("--credential-mode", choices=("bitwarden", "local"), default="bitwarden")
    args = parser.parse_args()

    ledger = load_listing_ledger()
    orders = fetch_orders(args.env, credential_mode=args.credential_mode)
    prepped = prep_orders(orders, ledger)

    needs_lookup = sum(
        1 for o in prepped for li in o["line_items"]
        if li["sourcing"]["status"] == "NEEDS_MANUAL_SOURCING_LOOKUP"
    )
    print(f"[branch11_orders] {args.env}: {len(prepped)} pending order(s), "
          f"{needs_lookup} line item(s) need manual sourcing lookup.")

    push_ledger = load_woo_push_ledger()
    pushed = already_pushed = skipped = errored = 0
    for order in prepped:
        result = push_order_to_woocommerce(order, push_ledger)
        order["woo_push"] = result
        status = result["status"]
        if status == "PUSHED":
            pushed += 1
            print(f"[branch11_orders] order {order['order_id']}: pushed to WooCommerce, woo_order_id={result['woo_order_id']}")
        elif status == "ALREADY_PUSHED":
            already_pushed += 1
        elif status == "SKIPPED_MISSING_SOURCING":
            skipped += 1
        else:
            errored += 1
            print(f"[branch11_orders] order {order['order_id']}: WooCommerce push FAILED -- {result.get('reason')}")
    print(f"[branch11_orders] WooCommerce push: {pushed} newly pushed, {already_pushed} already pushed, "
          f"{skipped} skipped (missing sourcing/mirror), {errored} errored.")

    out_path = OUT_DIR / f"branch11_orders_pending_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(prepped, indent=2))
    print(f"[branch11_orders] wrote {out_path}")


if __name__ == "__main__":
    main()
