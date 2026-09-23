#!/usr/bin/env python3
"""Branch 11 test suite: individual eBay API tests + end-to-end flow tests.

Runnable against either environment: `python3 branch11_ebay_tests.py --env sandbox --test all`

Safety, not just convention: tests that could mutate real data are listed in
MUTATING_TESTS and the runner refuses to execute them against production --
no flag overrides this. Read-only tests are safe on either environment,
though running them against production still touches Travis's real account
data (read-only, but real) -- prefer sandbox while iterating.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from ecommerce_listing_mgmt.ebay.auth import ENVIRONMENTS, api_base, refresh_access_token


class TestResult:
    def __init__(self, name: str, passed: bool, detail: str):
        self.name, self.passed, self.detail = name, passed, detail


def _api_call(env: str, method: str, path: str, params: dict | None = None, body: dict | None = None) -> tuple[int, dict | None]:
    token = refresh_access_token(env)["access_token"]
    url = f"{api_base(env)}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")}


# ---------------------------------------------------------------------------
# Individual API tests -- one per endpoint, read-only, safe on either env.
# ---------------------------------------------------------------------------

def test_oauth_refresh(env: str) -> TestResult:
    result = refresh_access_token(env)
    ok = bool(result.get("access_token")) and result.get("expires_in", 0) > 0
    return TestResult("oauth_refresh", ok, f"expires_in={result.get('expires_in')}")


def test_get_fulfillment_policies(env: str) -> TestResult:
    status, body = _api_call(env, "GET", "/sell/account/v1/fulfillment_policy", {"marketplace_id": "EBAY_US"})
    ok = status == 200 and body is not None and "fulfillmentPolicies" in body
    return TestResult("get_fulfillment_policies", ok, f"HTTP {status}, {len(body.get('fulfillmentPolicies', [])) if ok else 0} polic(ies)")


def test_get_payment_policies(env: str) -> TestResult:
    status, body = _api_call(env, "GET", "/sell/account/v1/payment_policy", {"marketplace_id": "EBAY_US"})
    ok = status == 200 and body is not None and "paymentPolicies" in body
    return TestResult("get_payment_policies", ok, f"HTTP {status}, {len(body.get('paymentPolicies', [])) if ok else 0} polic(ies)")


def test_get_return_policies(env: str) -> TestResult:
    status, body = _api_call(env, "GET", "/sell/account/v1/return_policy", {"marketplace_id": "EBAY_US"})
    ok = status == 200 and body is not None and "returnPolicies" in body
    return TestResult("get_return_policies", ok, f"HTTP {status}, {len(body.get('returnPolicies', [])) if ok else 0} polic(ies)")


def test_get_orders(env: str) -> TestResult:
    status, body = _api_call(env, "GET", "/sell/fulfillment/v1/order")
    ok = status == 200 and body is not None and "orders" in body
    return TestResult("get_orders", ok, f"HTTP {status}, total={body.get('total') if ok else '?'}")


def test_get_opted_in_programs(env: str) -> TestResult:
    status, body = _api_call(env, "GET", "/sell/account/v1/program/get_opted_in_programs")
    ok = status == 200 and body is not None
    programs = body.get("programs", []) if ok else []
    return TestResult("get_opted_in_programs", ok, f"HTTP {status}, programs={[p.get('programType') for p in programs]}")


def test_opt_in_business_policies(env: str) -> TestResult:
    """MUTATING -- sandbox test-user bootstrapping only. See MUTATING_TESTS."""
    status, body = _api_call(
        env, "POST", "/sell/account/v1/program/opt_in", body={"programType": "SELLING_POLICY_MANAGEMENT"}
    )
    ok = status in (200, 204)
    return TestResult("opt_in_business_policies", ok, f"HTTP {status}")


# ---------------------------------------------------------------------------
# End-to-end flow tests
# ---------------------------------------------------------------------------

def test_e2e_research_pipeline(env: str) -> TestResult:
    """Steps 1-4: discover eBay Deals candidates -> match on AliExpress ->
    judge -> profit calc. No eBay credentials involved -- `env` is accepted
    for CLI uniformity but unused here, this flow doesn't touch eBay's API
    at all yet (Steps 5/6/8 aren't wired into the pipeline's output yet --
    see next steps)."""
    from ecommerce_listing_mgmt.pipeline import run_pipeline

    results = run_pipeline(price_ceiling=100.0, fee_pct=0.17, margin_pct=0.15, limit=3)
    ok = len(results) > 0
    passing = sum(1 for r in results if r.profit_check and r.profit_check.get("passes"))
    return TestResult("e2e_research_pipeline", ok, f"{len(results)} candidates processed, {passing} passing")


def test_e2e_account_readiness(env: str) -> TestResult:
    """Chains OAuth -> Account API -> Fulfillment API -- confirms the whole
    read-only path Steps 5/6/8 depend on works together, not just that each
    endpoint responds in isolation."""
    steps = []
    try:
        token = refresh_access_token(env)["access_token"]
        steps.append("oauth:ok")
    except Exception as e:
        return TestResult("e2e_account_readiness", False, f"oauth failed: {e}")

    for label, fn in [("fulfillment_policies", test_get_fulfillment_policies),
                       ("orders", test_get_orders)]:
        r = fn(env)
        steps.append(f"{label}:{'ok' if r.passed else 'FAIL'}")
        if not r.passed:
            return TestResult("e2e_account_readiness", False, " -> ".join(steps) + f" ({r.detail})")

    return TestResult("e2e_account_readiness", True, " -> ".join(steps))


# ---------------------------------------------------------------------------
# Registry + safety gate
# ---------------------------------------------------------------------------

MUTATING_TESTS = {"opt_in_business_policies"}

TESTS = {
    "oauth_refresh": test_oauth_refresh,
    "get_fulfillment_policies": test_get_fulfillment_policies,
    "get_payment_policies": test_get_payment_policies,
    "get_return_policies": test_get_return_policies,
    "get_orders": test_get_orders,
    "get_opted_in_programs": test_get_opted_in_programs,
    "opt_in_business_policies": test_opt_in_business_policies,
    "e2e_research_pipeline": test_e2e_research_pipeline,
    "e2e_account_readiness": test_e2e_account_readiness,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", required=True, choices=ENVIRONMENTS, help="No default -- be explicit")
    ap.add_argument("--test", default="all", help="Comma-separated test names, or 'all' (default), or 'readonly' for all non-mutating tests")
    args = ap.parse_args()

    if args.test == "all":
        names = list(TESTS)
    elif args.test == "readonly":
        names = [n for n in TESTS if n not in MUTATING_TESTS]
    else:
        names = [n.strip() for n in args.test.split(",")]

    unknown = [n for n in names if n not in TESTS]
    if unknown:
        print(f"Unknown test(s): {unknown}. Available: {list(TESTS)}")
        sys.exit(1)

    blocked = [n for n in names if n in MUTATING_TESTS and args.env == "production"]
    if blocked:
        print(f"REFUSING to run mutating test(s) {blocked} against production. "
              f"No override exists for this -- run against sandbox instead.")
        names = [n for n in names if n not in blocked]

    print(f"Running {len(names)} test(s) against {args.env.upper()}\n")
    results = []
    for name in names:
        try:
            r = TESTS[name](args.env)
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
