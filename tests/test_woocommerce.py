#!/usr/bin/env python3
"""Branch 11 test suite: WooCommerce DSers-bridge store verification.

Runnable: `python3 branch11_woocommerce_tests.py --test all`

There's no separate sandbox/production split here like the eBay suite has --
this whole store IS a synthetic bridge, no real customer ever touches it, so
the safety concern isn't "don't run this against production" but "don't
leave test data lying around for DSers to pick up later." Every mutating
test cleans up (deletes) what it created, and tags it with a `zzz-test-`
prefix so leftover data from a failed cleanup is still obviously identifiable
and won't collide with real Branch 11 SKUs (`branch11-<id>`).
"""
from __future__ import annotations

import argparse
import sys
import time

from ecommerce_listing_mgmt.woocommerce import client as woo


class TestResult:
    def __init__(self, name: str, passed: bool, detail: str):
        self.name, self.passed, self.detail = name, passed, detail


# ---------------------------------------------------------------------------
# Individual API tests -- read-only, safe to run anytime.
# ---------------------------------------------------------------------------

def test_rest_api_reachable() -> TestResult:
    """Hits the REST API root over the public URL -- confirms Funnel routing,
    HTTPS, and pretty permalinks are all still working, not just the site
    itself being up."""
    status, body = woo._call("GET", "")
    ok = status == 200
    return TestResult("rest_api_reachable", ok, f"HTTP {status}")


def test_products_auth() -> TestResult:
    status, body = woo.list_products()
    ok = status == 200 and isinstance(body, list)
    return TestResult("products_auth", ok, f"HTTP {status}, {len(body) if ok else '?'} product(s)")


def test_orders_auth() -> TestResult:
    status, body = woo.list_orders()
    ok = status == 200 and isinstance(body, list)
    return TestResult("orders_auth", ok, f"HTTP {status}, {len(body) if ok else '?'} order(s)")


def test_system_status() -> TestResult:
    """WooCommerce's own health-check endpoint -- confirms the plugin
    itself reports healthy, not just that its REST routes respond."""
    status, body = woo.system_status()
    ok = status == 200 and isinstance(body, dict) and "environment" in body
    version = body.get("environment", {}).get("version") if ok else None
    return TestResult("system_status", ok, f"HTTP {status}, woocommerce version={version}")


# ---------------------------------------------------------------------------
# Mutating tests -- create real (throwaway) data, always clean up after.
# ---------------------------------------------------------------------------

def test_create_and_delete_product() -> TestResult:
    """Confirms the write path Task 3 (product/AliExpress mapping) depends
    on actually works, not just reads."""
    sku = f"zzz-test-{int(time.time())}"
    status, body = woo.create_product("Branch11 Test Product (safe to ignore)", sku, "9.99")
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
        return TestResult("create_and_delete_product", False, f"create failed: HTTP {status}, {body}")

    product_id = body["id"]
    del_status, del_body = woo.delete_product(product_id, force=True)
    cleanup_ok = del_status == 200
    return TestResult(
        "create_and_delete_product", cleanup_ok,
        f"created id={product_id} (HTTP {status}), deleted (HTTP {del_status})"
        if cleanup_ok else f"created id={product_id} but cleanup FAILED (HTTP {del_status}, {del_body}) -- delete manually"
    )


def test_create_and_delete_order() -> TestResult:
    """Confirms the write path Task 4 (pushing a pending eBay order in as a
    WooCommerce order) actually works. Uses status="pending" and a
    throwaway product -- never implies a real payment on this bridge store."""
    sku = f"zzz-test-{int(time.time())}"
    p_status, p_body = woo.create_product("Branch11 Test Product (safe to ignore)", sku, "9.99")
    if p_status not in (200, 201) or not isinstance(p_body, dict) or not p_body.get("id"):
        return TestResult("create_and_delete_order", False, f"prerequisite product create failed: HTTP {p_status}, {p_body}")
    product_id = p_body["id"]

    try:
        o_status, o_body = woo.create_order(
            line_items=[{"product_id": product_id, "quantity": 1}],
            billing={"first_name": "Branch11", "last_name": "Test", "address_1": "123 Test St",
                     "city": "Testville", "state": "CA", "postcode": "90001", "country": "US"},
        )
        if o_status not in (200, 201) or not isinstance(o_body, dict) or not o_body.get("id"):
            return TestResult("create_and_delete_order", False, f"order create failed: HTTP {o_status}, {o_body}")

        order_id = o_body["id"]
        od_status, od_body = woo.delete_order(order_id, force=True)
        cleanup_ok = od_status == 200
        detail = (f"created order id={order_id} (HTTP {o_status}), deleted (HTTP {od_status})"
                  if cleanup_ok else f"created order id={order_id} but cleanup FAILED (HTTP {od_status}, {od_body}) -- delete manually")
    finally:
        woo.delete_product(product_id, force=True)  # always attempt cleanup, even if the order step failed/raised

    return TestResult("create_and_delete_order", cleanup_ok, detail)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def test_e2e_bridge_readiness() -> TestResult:
    """Chains reachability -> auth -> a real write+cleanup -- confirms the
    whole path Task 3/4 will depend on works together, not just that each
    piece responds in isolation."""
    steps = []
    for label, fn in [("reachable", test_rest_api_reachable), ("products_auth", test_products_auth),
                       ("create_and_delete_product", test_create_and_delete_product)]:
        r = fn()
        steps.append(f"{label}:{'ok' if r.passed else 'FAIL'}")
        if not r.passed:
            return TestResult("e2e_bridge_readiness", False, " -> ".join(steps) + f" ({r.detail})")
    return TestResult("e2e_bridge_readiness", True, " -> ".join(steps))


TESTS = {
    "rest_api_reachable": test_rest_api_reachable,
    "products_auth": test_products_auth,
    "orders_auth": test_orders_auth,
    "system_status": test_system_status,
    "create_and_delete_product": test_create_and_delete_product,
    "create_and_delete_order": test_create_and_delete_order,
    "e2e_bridge_readiness": test_e2e_bridge_readiness,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", default="all", help="Comma-separated test names, or 'all' (default)")
    args = ap.parse_args()

    names = list(TESTS) if args.test == "all" else [n.strip() for n in args.test.split(",")]
    unknown = [n for n in names if n not in TESTS]
    if unknown:
        print(f"Unknown test(s): {unknown}. Available: {list(TESTS)}")
        sys.exit(1)

    print(f"Running {len(names)} test(s) against the WooCommerce bridge store\n")
    results = []
    for name in names:
        try:
            r = TESTS[name]()
        except Exception as e:
            r = TestResult(name, False, f"raised {type(e).__name__}: {e}")
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.detail}")

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
