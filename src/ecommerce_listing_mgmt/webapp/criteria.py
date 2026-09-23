"""Pure criteria evaluation: a matched candidate's numbers + the user's
settings -> pass/fail with one line per rule. No I/O, so it's the same
answer in the dashboard, the scheduled run, and the tests."""
from __future__ import annotations

from dataclasses import dataclass

from ecommerce_listing_mgmt.pipeline import compute_profit
from ecommerce_listing_mgmt.webapp.schemas import UserSettingsModel


@dataclass
class Evaluation:
    passes: bool
    list_price: float
    profit: float | None
    margin_pct: float | None
    rules: list[dict]


def list_price_for(ebay_price: float, s: UserSettingsModel) -> float:
    return round(ebay_price * (1 - s.price_undercut_pct / 100), 2)


def evaluate(*, ebay_price: float, match_score: int, cj_cost: float | None,
             shipping_cost: float | None, shipping_days_max: int | None,
             s: UserSettingsModel) -> Evaluation:
    rules: list[dict] = []

    def rule(name: str, ok: bool, detail: str) -> None:
        rules.append({"rule": name, "ok": bool(ok), "detail": detail})

    price = list_price_for(ebay_price, s)
    rule("eBay price range", s.min_ebay_price <= ebay_price <= s.max_ebay_price,
         f"${ebay_price:.2f} (allowed ${s.min_ebay_price:.2f}-${s.max_ebay_price:.2f})")
    rule("Match confidence", match_score >= s.min_match_score,
         f"score {match_score} (min {s.min_match_score})")

    profit = margin = None
    if cj_cost is None:
        rule("Supplier cost", False, "no CJ price available")
    if shipping_cost is None:
        rule("Shipping", False, f"no CJ shipping option within {s.max_delivery_days} days")
    else:
        rule("Shipping cost", shipping_cost <= s.max_shipping_cost,
             f"${shipping_cost:.2f} (max ${s.max_shipping_cost:.2f})")
        rule("Delivery time", shipping_days_max is not None and shipping_days_max <= s.max_delivery_days,
             f"up to {shipping_days_max} days (max {s.max_delivery_days})")

    if cj_cost is not None and shipping_cost is not None:
        p = compute_profit(price, cj_cost, s.fee_pct / 100, s.target_margin_pct / 100, ali_shipping=shipping_cost)
        profit = p["total_profit"]
        margin = round(100 * profit / price, 1) if price else None
        rule("Target margin", p["passes"],
             f"margin {margin}% after {s.fee_pct:g}% fees (target {s.target_margin_pct:g}%)")
        rule("Minimum profit", profit >= s.min_profit, f"${profit:.2f} (min ${s.min_profit:.2f})")

    return Evaluation(all(r["ok"] for r in rules), price, profit, margin, rules)
