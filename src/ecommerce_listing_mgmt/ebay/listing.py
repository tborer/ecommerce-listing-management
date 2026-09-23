#!/usr/bin/env python3
"""Branch 11 Step 7: eBay Inventory API -- create/publish/withdraw a listing.

Real listing creation. Every function here can create real (or, in sandbox,
fake-but-real-API) state on eBay -- treat every call as mutating. **As of
2026-08-27, `list_candidate()` IS wired to run automatically/unattended
against PRODUCTION for the top AUTO_LIST_TOP_N candidates each day** --
Travis's explicit decision, see AUTO_LIST_ENABLED/run_auto_listing() in
branch11_pipeline.py and branch11-listing-rules.md's "Autonomous production
publishing" section. Manual/interactive use (chat-approved SKUs,
credential_mode="bitwarden") remains available and unchanged.

Needs sell.inventory (full) scope, obtained via a separate re-consent from the
read-only scopes the rest of Branch 11 uses -- see branch11-listing-rules.md's
"eBay OAuth" section. Both sandbox and production have it as of 2026-08-27.
"""
from __future__ import annotations

import contextvars
import difflib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ecommerce_listing_mgmt.ebay.auth import api_base, refresh_access_token, refresh_access_token_unattended

# Travis's decision, 2026-08-26: use this one return policy for every
# production listing, regardless of category/item -- not tiered like
# shipping. Production-only; sandbox has no equivalent (createReturnPolicy
# is broken there, and return_policy_id is optional for offers anyway --
# see this module's create_offer()).
PRODUCTION_RETURN_POLICY_ID = "241134673013"  # "TB Return Policy"


# credential_mode="context" (2026-09-23, the web app): the calling request
# has already minted the logged-in user's own eBay access token and set it
# here, so every helper in this module acts on that user's account.
USER_ACCESS_TOKEN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ebay_user_access_token", default=None)


def _call(env: str, method: str, path: str, body: dict | None = None,
          credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """`credential_mode`: "bitwarden" (default, interactive, needs BW_SESSION --
    used everywhere Travis is present) or "local" (unattended auto-listing
    path only, branch11_pipeline.py's run_auto_listing() -- reads
    branch11_unattended_creds.json instead, see branch11_ebay_auth.py)."""
    if credential_mode == "context":
        token = USER_ACCESS_TOKEN.get()
        if not token:
            raise RuntimeError("credential_mode='context' but no USER_ACCESS_TOKEN is set")
    elif credential_mode == "local":
        token = refresh_access_token_unattended(env)["access_token"]
    else:
        token = refresh_access_token(env)["access_token"]
    url = f"{api_base(env)}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        },
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


def _error_detail(status: int, body: dict | None) -> str:
    """Real eBay error detail from a failed _call() response, for error
    messages -- 2026-08-28: a prior generic message ("known to be broken in
    sandbox") on publish_offer failures masked a completely different, real,
    fixable production error (missing required item aspect) for a full day.
    Never hardcode an assumed cause again; always surface what eBay actually said."""
    if isinstance(body, dict):
        errs = body.get("errors")
        if errs:
            msgs = "; ".join(f"errorId {e.get('errorId')}: {e.get('message')}" for e in errs)
            return f"HTTP {status} -- {msgs}"
    return f"HTTP {status} -- {body}"


def ensure_test_policies(env: str, credential_mode: str = "bitwarden") -> dict:
    """Sandbox test accounts start with zero business policies -- offers can't
    be created without at least one fulfillment/payment/return policy. Creates
    minimal ones if none exist; returns their IDs. Idempotent-ish: if policies
    already exist, reuses the first of each rather than creating duplicates."""
    result = {}

    status, body = _call(env, "GET", "/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US", credential_mode=credential_mode)
    policies = body.get("fulfillmentPolicies", []) if status == 200 else []
    if policies:
        result["fulfillmentPolicyId"] = policies[0]["fulfillmentPolicyId"]
    else:
        status, body = _call(env, "POST", "/sell/account/v1/fulfillment_policy", {
            "name": "Branch11 Test Fulfillment Policy",
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES", "default": True}],
            "handlingTime": {"value": 3, "unit": "DAY"},
            "shippingOptions": [{
                "optionType": "DOMESTIC", "costType": "FLAT_RATE",
                "shippingServices": [{
                    "sortOrder": 1, "shippingCarrierCode": "USPS", "shippingServiceCode": "USPSPriority",
                    "shippingCost": {"value": "9.99", "currency": "USD"},
                }],
            }],
        }, credential_mode=credential_mode)
        if status not in (200, 201):
            raise RuntimeError(f"createFulfillmentPolicy failed: HTTP {status} {body}")
        result["fulfillmentPolicyId"] = body["fulfillmentPolicyId"]

    status, body = _call(env, "GET", "/sell/account/v1/payment_policy?marketplace_id=EBAY_US", credential_mode=credential_mode)
    policies = body.get("paymentPolicies", []) if status == 200 else []
    if policies:
        result["paymentPolicyId"] = policies[0]["paymentPolicyId"]
    else:
        status, body = _call(env, "POST", "/sell/account/v1/payment_policy", {
            "name": "Branch11 Test Payment Policy",
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES", "default": True}],
            "immediatePay": False,
        }, credential_mode=credential_mode)
        if status not in (200, 201):
            raise RuntimeError(f"createPaymentPolicy failed: HTTP {status} {body}")
        result["paymentPolicyId"] = body["paymentPolicyId"]

    status, body = _call(env, "GET", "/sell/account/v1/return_policy?marketplace_id=EBAY_US", credential_mode=credential_mode)
    policies = body.get("returnPolicies", []) if status == 200 else []
    if policies:
        result["returnPolicyId"] = policies[0]["returnPolicyId"]
    else:
        status, body = _call(env, "POST", "/sell/account/v1/return_policy", {
            "name": "Branch11 Test Return Policy",
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES", "default": True}],
            "returnsAccepted": True,
            "returnPeriod": {"value": 30, "unit": "DAY"},
            "returnShippingCostPayer": "BUYER",
        }, credential_mode=credential_mode)
        if status not in (200, 201):
            raise RuntimeError(f"createReturnPolicy failed: HTTP {status} {body}")
        result["returnPolicyId"] = body["returnPolicyId"]

    return result


def ebay_listing_title(ali_title: str, max_len: int = 80) -> str:
    """Truncate an AliExpress title to fit eBay's 80-char listing title limit,
    cutting at a word boundary rather than mid-word. Travis's 2026-08-27
    decision: listings must use the AliExpress supplier's own title, not the
    eBay Deals-page item's title -- using the eBay item's title risks naming
    a specific brand/model (e.g. "Samsung Galaxy Buds3 Pro") for a sourced
    item that isn't actually that product, which is exactly the failure mode
    the match-judgment rubric's brand-mismatch cases exposed (see
    branch11-listing-rules.md, 2026-08-27). The AliExpress title describes
    what's actually being shipped, so using it keeps the listing honest even
    when the research-stage match itself was imperfect."""
    title = ali_title.strip()
    if len(title) <= max_len:
        return title
    cut = title[:max_len].rsplit(" ", 1)[0]
    return cut if cut else title[:max_len]


_SCRUB_TERMS = ("AliExpress", "Free", "Returns", "Return", "Worldwide")
_SCRUB_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in _SCRUB_TERMS) + r")\b", re.IGNORECASE)


def scrub_description(text: str) -> str:
    """Strip terms that must never reach a live eBay description -- "AliExpress"
    (reveals the dropship source), "Free" (risks an unintended free-shipping/
    free-item claim eBay didn't actually configure on this listing), "Returns"/
    "Return" (avoids the description making a returns claim that conflicts with
    the account's actual return policy), and "Worldwide" (avoids implying
    worldwide shipping when the actual shipping policy may not cover that).
    Travis's 2026-08-29 decision, extended same day after live listings showed
    "Shipping Worldwide"/"Easy Return" boilerplate had slipped through under the
    original 3-term list. Whole-word, case-insensitive (so "Freedom" survives
    untouched, and "Return"/"Returns" don't overlap-match each other), with the
    leftover whitespace/punctuation collapsed afterward so removal doesn't
    leave double spaces or a dangling comma."""
    scrubbed = _SCRUB_RE.sub("", text)
    scrubbed = re.sub(r"[ \t]{2,}", " ", scrubbed)
    scrubbed = re.sub(r"[ \t]+([,.;:!?])", r"\1", scrubbed)
    return scrubbed.strip()


def create_inventory_item(env: str, sku: str, title: str, description: str,
                           image_url: str, quantity: int = 1, condition: str = "NEW",
                           aspects: dict | None = None, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """`aspects` (see build_item_aspects()) fills the category's required
    item specifics (Brand/Type/Size/etc.) -- omitting them for a category
    that requires them fails later, at publish_offer, with a confusing
    "item specific X is missing" error (confirmed live 2026-08-28)."""
    product = {"title": title, "description": description, "imageUrls": [image_url]}
    if aspects:
        product["aspects"] = aspects
    body = {
        "product": product,
        "condition": condition,
        "availability": {"shipToLocationAvailability": {"quantity": quantity}},
    }
    return _call(env, "PUT", f"/sell/inventory/v1/inventory_item/{sku}", body, credential_mode=credential_mode)


def create_offer(env: str, sku: str, price: float, category_id: str,
                  fulfillment_policy_id: str, payment_policy_id: str, return_policy_id: str | None = None,
                  marketplace_id: str = "EBAY_US", currency: str = "USD",
                  listing_description: str = "Branch 11 sandbox test listing -- not a real item.",
                  credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """return_policy_id is optional -- confirmed 2026-08-26 that eBay's sandbox
    accepts an offer without one for at least this category (createOffer
    succeeded, 201). Not required due to a real eBay sandbox bug in
    createReturnPolicy itself (consistent 400 "Internal server error" even on
    a minimal payload -- matches reports on eBay's own developer forums, not a
    problem with this module's request shape). Production already has real
    return policies from Travis's existing account, so this doesn't affect
    production at all -- pass return_policy_id there once Step 7 is wired to
    production."""
    listing_policies = {
        "fulfillmentPolicyId": fulfillment_policy_id,
        "paymentPolicyId": payment_policy_id,
    }
    if return_policy_id:
        listing_policies["returnPolicyId"] = return_policy_id
    body = {
        "sku": sku,
        "marketplaceId": marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": category_id,
        "listingDescription": listing_description,
        "listingPolicies": listing_policies,
        "pricingSummary": {"price": {"value": str(price), "currency": currency}},
        "merchantLocationKey": "default",
    }
    return _call(env, "POST", "/sell/inventory/v1/offer", body, credential_mode=credential_mode)


def publish_offer(env: str, offer_id: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    return _call(env, "POST", f"/sell/inventory/v1/offer/{offer_id}/publish", credential_mode=credential_mode)


def get_offer(env: str, offer_id: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    return _call(env, "GET", f"/sell/inventory/v1/offer/{offer_id}", credential_mode=credential_mode)


def withdraw_offer(env: str, offer_id: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    return _call(env, "POST", f"/sell/inventory/v1/offer/{offer_id}/withdraw", credential_mode=credential_mode)


def delete_inventory_item(env: str, sku: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    return _call(env, "DELETE", f"/sell/inventory/v1/inventory_item/{sku}", credential_mode=credential_mode)


# 2026-09-04 (PRODUCTION-READINESS.md Item 25, Phase 3): eBay Variations API
# support for genuinely multi-variant candidates. Real mechanics confirmed
# 2026-08-31 (see that item's write-up): each variant SKU must already exist
# as its own inventory item (create_inventory_item(), unchanged, called once
# per variant value) before create_inventory_item_group() links them; each
# variant SKU also needs its own offer (create_offer(), unchanged) before
# publish_offer_by_inventory_item_group() replaces the single-offer
# publish_offer() call, publishing every offer in the group together as one
# multi-variant listing under one listingId. Real quantified data (that
# item's write-up) found only two aspects worth building this for --
# `Color` and `Number of Shelves` -- so this stays single-pivot-aspect
# (varies_by_aspect: str, not a list) until real data ever calls for more;
# eBay's own schema supports multiple simultaneous pivot aspects if that
# ever changes.
def create_inventory_item_group(env: str, group_key: str, title: str, description: str,
                                 image_urls: list[str], variant_skus: list[str],
                                 varies_by_aspect: str, varies_by_values: list[str],
                                 common_aspects: dict | None = None,
                                 credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """Member SKUs (variant_skus) must already exist as inventory items --
    creating the group before they exist fails (eBay requirement, per its
    own docs). varies_by_values must cover every value actually present
    across variant_skus's own per-item aspects, or eBay's later publish
    call rejects the group as inconsistent.

    `common_aspects` -- confirmed live 2026-09-04 (real sandbox mechanism
    test, PRODUCTION-READINESS.md Item 25 Phase 3): every required aspect
    OTHER than varies_by_aspect must be repeated here explicitly, even
    though each member inventory item already carries its own copy.
    Omitting it (assuming the group inherits from member items) fails
    publish_offer_by_inventory_item_group() with "item specific X is
    missing" for every non-varying required aspect -- confirmed live, not
    a guess. Pass the same aspects dict used for the member items, minus
    the varies_by_aspect key."""
    body = {
        "title": title,
        "description": description,
        "imageUrls": image_urls,
        "variantSKUs": variant_skus,
        "variesBy": {
            "specifications": [{"name": varies_by_aspect, "values": varies_by_values}],
            "aspectsImageVariesBy": [varies_by_aspect],
        },
    }
    if common_aspects:
        body["aspects"] = common_aspects
    return _call(env, "PUT", f"/sell/inventory/v1/inventory_item_group/{group_key}", body, credential_mode=credential_mode)


def get_inventory_item_group(env: str, group_key: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    return _call(env, "GET", f"/sell/inventory/v1/inventory_item_group/{group_key}", credential_mode=credential_mode)


def delete_inventory_item_group(env: str, group_key: str, credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """Deletes the group only -- does not delete member inventory items,
    same asymmetry as eBay's own API (mirrors delete_inventory_item() being
    a separate call)."""
    return _call(env, "DELETE", f"/sell/inventory/v1/inventory_item_group/{group_key}", credential_mode=credential_mode)


def publish_offer_by_inventory_item_group(env: str, group_key: str, marketplace_id: str = "EBAY_US",
                                           credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    body = {"inventoryItemGroupKey": group_key, "marketplaceId": marketplace_id}
    return _call(env, "POST", "/sell/inventory/v1/offer/publish_by_inventory_item_group", body, credential_mode=credential_mode)


def withdraw_offer_by_inventory_item_group(env: str, group_key: str, marketplace_id: str = "EBAY_US",
                                            credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    body = {"inventoryItemGroupKey": group_key, "marketplaceId": marketplace_id}
    return _call(env, "POST", "/sell/inventory/v1/offer/withdraw_by_inventory_item_group", body, credential_mode=credential_mode)


def suggest_category(env: str, title: str, credential_mode: str = "bitwarden") -> str | None:
    """Best-effort category ID from eBay's Taxonomy API, using the item title
    as the query -- same mechanism used 2026-08-26 to find a low-friction test
    category. Needs the base `api_scope` (not just sell.* scopes) -- confirmed
    present for sandbox; NOT yet requested for production (see module docstring
    and branch11-listing-rules.md), so this will 403 against production until
    that's added. Not guaranteed accurate -- a real category-matching pass
    (e.g. checking the AliExpress match's own category, or a human glance)
    is worth adding before trusting this at scale; treat as a reasonable
    default, not a verified-correct category.
    """
    status, body = _call(env, "GET",
                          f"/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q={urllib.parse.quote(title)}",
                          credential_mode=credential_mode)
    if status != 200:
        return None
    suggestions = body.get("categorySuggestions", [])
    return suggestions[0]["category"]["categoryId"] if suggestions else None


def get_required_aspects(env: str, category_id: str, credential_mode: str = "bitwarden") -> list[dict]:
    """Required item-specific aspects for a category, per eBay's Taxonomy API.
    `publish_offer` fails (errorId 25002, "item specific X is missing") if
    any required aspect isn't supplied on the inventory item -- confirmed
    live 2026-08-28 across two real production categories (117042: Brand,
    Type; 175751: Brand, Size, Type). Returns each aspect's full
    eBay-suggested allowed-values list (not a truncated sample) so callers
    can match against real item text rather than guessing."""
    status, body = _call(env, "GET",
                          f"/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category?category_id={category_id}",
                          credential_mode=credential_mode)
    if status != 200:
        return []
    required = []
    for a in (body or {}).get("aspects", []):
        constraint = a.get("aspectConstraint", {})
        if constraint.get("aspectRequired"):
            required.append({
                "name": a.get("localizedAspectName"),
                "values": [v.get("localizedValue") for v in a.get("aspectValues", []) if v.get("localizedValue")],
                # Real eBay signal for "this aspect is a product-variation
                # dimension" (Size, Color, etc.), not a name we'd have to
                # hardcode -- confirmed live 2026-08-28 (category 175751:
                # Size AND Type both True, Brand False). Used by
                # analyze_category() to decide whether an unresolvable
                # aspect specifically means "needs eBay Variations API
                # support we don't have" vs. some other kind of gap.
                "enabled_for_variations": constraint.get("aspectEnabledForVariations", False),
            })
    return required


def _generic_aspect_value(name: str, values: list[str]) -> str | None:
    """The one safe, eBay-suggested generic value for a required aspect, if
    any -- shared by build_item_aspects() and analyze_category() so both
    agree on exactly what counts as "safely resolvable" (see
    build_item_aspects()'s docstring for why this never matches against item
    text). Brand -> "Unbranded" if eBay offers it; anything else ->
    "Does Not Apply"/"Not Specified"/"Other" if eBay offers one of those."""
    if name == "Brand":
        unbranded = next((v for v in values if v.lower() == "unbranded"), None)
        if unbranded:
            return unbranded
    return next((v for v in values if v.lower() in ("does not apply", "not specified", "other")), None)


# 2026-08-29: basic variant handling, first pass -- see
# branch11-listing-rules.md's "Basic variant handling" section for the full
# reasoning and real worked examples (an iPad folio case's Type, a grill's
# Model) this was built and validated against. Same compatibility-qualifier
# concept as branch11_pipeline.py's _COMPAT_QUALIFIERS/
# _brand_authenticity_mismatch() (duplicated here, not imported, to avoid a
# circular import between the two modules) -- "for X"/"compatible with X"
# means X is a target this item works with, not a true attribute/identity
# of the item itself.
_ASPECT_COMPAT_QUALIFIERS = {"for", "fits", "fit", "compatible", "replacement", "universal"}

# 2026-08-31: found live -- three real HIGH-verdict, profit-passing mouth-tape
# candidates all failed aspect resolution on category 40101's ("Other Sleeping
# Aids") required "Type" aspect, whose only two eBay-offered values are
# "Anti-Snoring Wearable" and "Sleep Aid Aromatherapy". _value_present_in_text()
# correctly found neither phrase *literally* in the Ali match's title/
# description (which say things like "Anti Snoring Sleep Mouth Tape" /
# "Prevent Snoring" -- obviously the same thing, just not that exact string),
# so a case that's actually unambiguous kept getting flagged for manual
# review. Same "deliberately blunt, explainable, narrow" pattern as
# _BRAND_TOKENS/_ACCESSORY_WORDS in branch11_pipeline.py: a small hand-curated
# keyword set per (aspect name, exact eBay-offered value string) -- fires only
# for this exact pair, so an unmapped aspect/value correctly still falls
# through to "can't resolve" rather than silently guessing. Extend this dict
# (never loosen _value_present_in_text itself) as new easy-but-missed cases
# turn up the same way the other curated lists were built.
_ASPECT_VALUE_SYNONYMS: dict[tuple[str, str], set[str]] = {
    ("Type", "Anti-Snoring Wearable"): {"snor"},  # stem covers snore/snoring/anti-snoring
    ("Type", "Sleep Aid Aromatherapy"): {"aromatherapy", "essential oil", "diffuser"},
}

# 2026-08-31 (PRODUCTION-READINESS.md Item 26): real-dimension resolution for
# Item Length/Height/Width/Depth/Diameter-style required aspects. Confirmed
# live that AliExpress product pages frequently state real physical
# dimensions as either a Specifications-table key/value pair (e.g. "Base to
# top distance" / "44cm") or in ordinary body-page prose (e.g. "the table
# measures 26.5cm long, 16.8cm wide, and 11cm high") -- neither needs OCR;
# `PRODUCT_PAGE_JS` (branch11_pipeline.py) captures both into
# `AliMatch.dimension_text` during the same page visit already used for
# description/price. Deliberately narrow, context-word-based, same evidence
# bar as every other tier in this module: a measurement only counts for a
# given dimension if it's paired -- in the same spec-table row, or the same
# comma-split clause -- with one of that dimension's own context words,
# never just "the first number found anywhere."
_DIMENSION_CONTEXT_WORDS: dict[str, set[str]] = {
    # "base to top" (added 2026-08-31, found live): a recurring real
    # AliExpress spec-table key for folding tables/stands ("Base to top
    # distance" / "44cm") -- a bare "distance" was deliberately left out as
    # too generic/ambiguous on its own (could mean any axis), but this exact
    # multi-word phrase is specific enough to safely mean height.
    "Item Height": {"height", "tall", "high", "base to top"},
    "Item Length": {"length", "long"},
    "Item Width": {"width", "wide"},
    "Item Depth": {"depth", "deep"},
    "Item Diameter": {"diameter"},
}

_MEASUREMENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(cm|mm|in\.?|inches?|\")", re.IGNORECASE)
_DIMENSION_BUCKET_RE = re.compile(r"^\d+\s*in$", re.IGNORECASE)
_UNIT_TO_INCHES = {"cm": 1 / 2.54, "mm": 1 / 25.4, "in": 1.0, "in.": 1.0, "inch": 1.0, "inches": 1.0, '"': 1.0}


def _clause_measurement_inches(clause: str, context_words: set[str]) -> float | None:
    """The measurement (in inches) in `clause`, if the clause also contains
    one of `context_words` and has EXACTLY ONE measurement -- a clause with
    zero or multiple measurements is ambiguous, not resolved."""
    if not any(w in clause.lower() for w in context_words):
        return None
    matches = _MEASUREMENT_RE.findall(clause)
    if len(matches) != 1:
        return None
    value, unit = matches[0]
    factor = _UNIT_TO_INCHES.get(unit.lower().rstrip("."))
    return float(value) * factor if factor else None


def _extract_dimension_inches(aspect_name: str, dimension_text: str) -> float | None:
    """Parses `dimension_text` (branch11_pipeline.py's captured
    Specifications text + dimension-bearing body lines, newline-joined) for
    a single, unambiguous real measurement matching `aspect_name`'s
    dimension. Returns inches, or None if nothing safely resolvable (no
    evidence, or conflicting/multiple distinct candidate values -- never
    guesses between them)."""
    context_words = _DIMENSION_CONTEXT_WORDS.get(aspect_name)
    if not context_words or not dimension_text:
        return None

    candidates: set[float] = set()
    lines = [ln.strip() for ln in dimension_text.split("\n") if ln.strip()]

    # Spec-table pass: alternating Key/Value lines, confirmed live format
    # (e.g. "Specifications\nBase to top distance\n44cm\n...").
    for i in range(len(lines) - 1):
        key, value = lines[i].lower(), lines[i + 1]
        if any(w in key for w in context_words) and not _MEASUREMENT_RE.search(key):
            m = _MEASUREMENT_RE.findall(value)
            if len(m) == 1:
                num, unit = m[0]
                factor = _UNIT_TO_INCHES.get(unit.lower().rstrip("."))
                if factor:
                    candidates.add(round(float(num) * factor, 2))

    # Body-text pass: comma/period/semicolon-split clauses, e.g. "...measures
    # 26.5cm long, 16.8cm wide, and 11cm high".
    for line in lines:
        for clause in re.split(r",|;|\.(?!\d)", line):
            inches = _clause_measurement_inches(clause, context_words)
            if inches is not None:
                candidates.add(round(inches, 2))

    return candidates.pop() if len(candidates) == 1 else None


def _value_present_in_text(value: str, text: str) -> bool:
    """Whole-word (token-sequence), case-insensitive check for `value`
    inside `text`, excluding an occurrence immediately preceded by a
    compatibility qualifier -- e.g. "Shock Mount for Blue Yeti Mic" should
    never read "Blue" as this item's own Color, since Blue Yeti is a
    different product the item merely fits."""
    value_words = re.findall(r"[a-z0-9]+", value.lower())
    if not value_words:
        return False
    text_tokens = re.findall(r"[a-z0-9]+", text.lower())
    n = len(value_words)
    for i in range(len(text_tokens) - n + 1):
        if text_tokens[i:i + n] == value_words:
            if i > 0 and text_tokens[i - 1] in _ASPECT_COMPAT_QUALIFIERS:
                continue
            return True
    return False


def _value_words_scattered_present(value: str, text: str) -> bool:
    """Looser cousin of `_value_present_in_text()`: same evidence bar --
    every significant word of `value` must literally appear in `text`, and
    never immediately after a compatibility qualifier -- but doesn't require
    the words to appear as one consecutive, in-order phrase.

    Added 2026-08-31 (PRODUCTION-READINESS.md Item 25, phase 1): found live
    while researching Variations API support that a large fraction of
    `needs_unresolvable_variation` candidates aren't genuinely multi-variant
    at all -- they're single-value cases where eBay's own enum value is
    itself multi-word or slash-compound (e.g. "Laptop Stand/Riser",
    "Portfolio Case/Bag") and the real AliExpress source describes the same
    concept in a different word order or with words in between ("...Wooden
    Laptop Stand - Ergonomic...Compact Bamboo Desktop Riser..."). Exact-
    phrase matching (`_value_present_in_text`) correctly finds nothing there
    even though the concept is obviously present -- confirmed live this was
    the actual reason several real candidates sat unresolved.

    **Guards against false positives from short/generic words**: only counts
    words of length >= 3 (so a value like "S Hook" can't spuriously match a
    stray "S" character elsewhere in the text); if a value has no qualifying
    word left after that filter, this always returns False -- a safe
    fall-through to "unresolved," never a guess built on nothing. Caller
    (`_resolve_aspect_value()`) additionally requires this to uniquely
    identify exactly one of eBay's values before trusting it -- this tier is
    looser than exact-phrase matching, so ambiguity is a more realistic risk
    here than there."""
    significant = [w for w in re.findall(r"[a-z0-9]+", value.lower()) if len(w) >= 3]
    if not significant:
        return False
    text_tokens = re.findall(r"[a-z0-9]+", text.lower())
    for word in significant:
        if not any(
            tok == word and not (i > 0 and text_tokens[i - 1] in _ASPECT_COMPAT_QUALIFIERS)
            for i, tok in enumerate(text_tokens)
        ):
            return False
    return True


def resolve_variant_values(ebay_values: list[str], dsers_values: list[str]) -> dict[str, str]:
    """Maps each DSers-supplied variant value to a confidently-identified
    real eBay-offered value, for Item 25 Phase 3 (eBay Variations API).

    **Real finding, 2026-09-04, from actually inspecting live captured
    data (`branch11_enrichment.json`), not assumed**: DSers's generic
    "options" field is NOT reliably genuine values for the aspect it's
    labeled under -- a real "Color" option group's values included things
    like "20V 2PCS 8Ah-Charger" (a battery-pack configuration) and "30mm
    multiple Open" (a size/style descriptor), not colors at all. Treating
    these as real eBay Color values would create an honestly wrong,
    confusing listing -- worse than skipping. So every DSers value must
    independently earn its place via the exact same evidence-based
    text-matching already trusted for single-SKU aspect resolution
    (`_value_present_in_text` then `_value_words_scattered_present`,
    checking whether a real eBay-offered value's words are actually
    present in the DSers value's own text) -- never guessed, never
    assumed correct just because DSers labeled the group with the right
    aspect name.

    Drops (never guesses) any DSers value that doesn't map to exactly one
    eBay value -- a smaller, honest variant group beats a wrong or
    refused one. Returns {} (not a genuine multi-variant case) unless at
    least 2 DSers values map to 2 DISTINCT eBay values -- two DSers labels
    collapsing to the same eBay value isn't a second real variant."""
    mapping: dict[str, str] = {}
    for dv in dsers_values:
        matches = [ev for ev in ebay_values if _value_present_in_text(ev, dv)]
        if not matches:
            matches = [ev for ev in ebay_values if _value_words_scattered_present(ev, dv)]
        if len(matches) == 1:
            mapping[dv] = matches[0]
    if len(set(mapping.values())) < 2:
        return {}
    return mapping


def _extract_model_token(text: str) -> str | None:
    """Free-text fallback, "Model" aspect only -- for categories where
    eBay's own suggested `values` sample is clearly just OTHER listings'
    unrelated model numbers, not a closed enum for this item at all
    (confirmed live 2026-08-29: a real espresso machine and a real grill
    both had a required, variation-enabled "Model" aspect whose suggested
    values were entirely different brands' model numbers -- genuinely an
    open text field, not a list to pick from). Returns the first
    alphanumeric, letter-and-digit-mixed token (a real model/part-number
    shape, e.g. "AG301", "EC9665.M") in `text` not immediately preceded by a
    compatibility qualifier -- pulled from the actual item's own real title,
    never a guess and never borrowed from a different product it just
    happens to mention fitting."""
    for m in re.finditer(r"[A-Za-z0-9]{3,12}", text):
        tok = m.group(0)
        if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
            continue
        preceding = text[:m.start()].strip().lower().split()
        if preceding and preceding[-1] in _ASPECT_COMPAT_QUALIFIERS:
            continue
        return tok
    return None


def _resolve_aspect_value(name: str, values: list[str], ali_text: str, dimension_text: str = "") -> str | None:
    """Extends `_generic_aspect_value()` with two narrow, evidence-based
    tiers for aspects that aren't Brand:

    1. Closed-enum tier: if one of eBay's OWN suggested values for this
       aspect literally appears in `ali_text` (the AliExpress match's own
       title+description -- the actual item being shipped, never the eBay
       comparable) -- use it. Still only ever a value eBay itself already
       vouches for as valid for this category; just sourced from real item
       text instead of always falling back to a fixed generic.
    1a. Scattered-word tier (added 2026-08-31, Item 25 phase 1): if tier 1's
        exact-phrase match found nothing, try `_value_words_scattered_present()`
        -- same words, same evidence bar, just doesn't require them
        consecutive/in-order. Only accepted if it uniquely identifies exactly
        one of eBay's values; otherwise falls through unresolved.
    1b. Curated-synonym tier (added 2026-08-31): if tier 1/1a found nothing,
        try `_ASPECT_VALUE_SYNONYMS` -- a small hand-curated keyword set per
        exact (aspect name, eBay-offered value) pair, for cases where the
        correct value is unambiguous from context but its exact phrase
        rarely appears verbatim (e.g. "Anti Snoring Sleep Mouth Tape" never
        literally says "Anti-Snoring Wearable"). Only fires for pairs
        explicitly curated there; still only ever picks a value eBay itself
        offered. If more than one of eBay's values matches by synonym, that's
        treated as genuinely ambiguous -- falls through unresolved rather
        than guessing between them.
    1c. Dimension tier (added 2026-08-31, Item 26): for `Item Length/Height/
        Width/Depth/Diameter`-style aspects whose values are all an "N in"
        bucket list, parses `dimension_text` (see `_extract_dimension_inches()`)
        for a real physical measurement, rounds to the nearest whole inch,
        and uses it only if that exact bucket is one of eBay's offered
        values -- never force-fits to the nearest available boundary if the
        real measurement falls outside eBay's offered range.
    2. Free-text tier, "Model" only: see `_extract_model_token()`.

    **Brand is deliberately excluded from both tiers** -- brand is an
    identity/provenance claim, not a physical attribute, and text-matching
    it already caused a real incident (2026-08-28, see
    build_item_aspects()'s docstring: "Sony" wrongly matched as Brand from
    "compatible with Sony PS5" text on a third-party accessory). These tiers
    are safe specifically because Color/Connectivity/Type/Model describe
    the item itself, not who made it."""
    generic = _generic_aspect_value(name, values)
    if generic is not None:
        return generic
    if name == "Brand":
        return None
    for v in values:
        if _value_present_in_text(v, ali_text):
            return v
    scattered_matches = [v for v in values if _value_words_scattered_present(v, ali_text)]
    if len(scattered_matches) == 1:
        return scattered_matches[0]
    synonym_matches = [
        v for v in values
        if any(kw in ali_text.lower() for kw in _ASPECT_VALUE_SYNONYMS.get((name, v), ()))
    ]
    if len(synonym_matches) == 1:
        return synonym_matches[0]
    if name in _DIMENSION_CONTEXT_WORDS and dimension_text and all(_DIMENSION_BUCKET_RE.match(v) for v in values):
        inches = _extract_dimension_inches(name, dimension_text)
        if inches is not None:
            bucket = f"{round(inches)} in"
            if bucket in values:
                return bucket
    if name == "Model":
        return _extract_model_token(ali_text)
    return None


def build_item_aspects(env: str, category_id: str, title_text: str,
                        credential_mode: str = "bitwarden", dimension_text: str = "") -> dict | None:
    """Builds the `product.aspects` dict needed to satisfy a category's
    required item specifics (see get_required_aspects()), or None if any
    required aspect can't be safely resolved -- caller should then flag for
    manual review, never guess a value eBay didn't suggest.

    **Never matches BRAND against title/description text.** First version
    tried that and was caught live 2026-08-28 before touching any real
    listing: it matched "Sony" as Brand for a third-party PS5 *accessory*
    just because the description said "compatible with Sony PS5" (wrong --
    the item isn't made by Sony), and matched "ED" as Brand inside the
    unrelated word "Bed". A wrong Brand/attribute on a real live listing is
    worse than not auto-listing it, so Brand only ever uses "Unbranded" (the
    correct, honest value for this project's AliExpress-sourced generic
    goods) or another eBay-offered generic ("Does Not Apply"/etc.).

    **Non-Brand aspects (Color/Connectivity/Type/Model/etc.), added
    2026-08-29** now also try `_resolve_aspect_value()`'s text-matching
    tiers against `title_text` -- see that function's docstring and
    branch11-listing-rules.md's "Basic variant handling" section for the
    full reasoning and real worked examples. This is safe where the Sony
    incident wasn't: these describe the item itself (what it looks like,
    how it connects, its part number), not who made it, so a value eBay
    itself already vouches for as valid for this category, found literally
    stated in the actual AliExpress source's own title/description, is a
    real fact about the item being shipped -- not a guess.

    Still returns None for the WHOLE result (not a partial dict) if any
    required aspect can't be resolved by any tier -- a category requiring a
    genuinely specific aspect with no generic option and no textual evidence
    can't be safely auto-filled at all. Caller must not cherry-pick the
    aspects that did resolve.
    """
    required = get_required_aspects(env, category_id, credential_mode=credential_mode)
    if not required:
        return {}
    aspects: dict[str, list[str]] = {}
    for aspect in required:
        value = _resolve_aspect_value(aspect["name"], aspect["values"], title_text, dimension_text)
        if value is None:
            return None
        aspects[aspect["name"]] = [value]
    return aspects


def analyze_category(env: str, title: str, credential_mode: str = "bitwarden", ali_text: str = "",
                      dimension_text: str = "") -> dict | None:
    """One-stop category analysis for a candidate, run during research (not
    just at listing time) so both the daily email and auto-list selection
    can use it -- Travis's 2026-08-28 request. Returns None only if the
    category-suggestion API call itself fails.

    `dimension_text` (added 2026-08-31, Item 26): the same product-page
    visit's captured Specifications-table + dimension-bearing body text (see
    `AliMatch.dimension_text`) -- passed through to `_resolve_aspect_value()`'s
    dimension tier so a real physical measurement can resolve an Item
    Length/Height/Width/etc. aspect the same way it does at listing time.

    `ali_text` (added 2026-08-29): the AliExpress match's own title (+
    description if available) -- the actual item that would ship, never the
    eBay comparable `title` param above (that's only ever used for category
    *suggestion*, matching how `list_candidate()` already keeps the two
    separate). Passed through to `_resolve_aspect_value()` so
    `needs_unresolvable_variation` reflects the SAME resolution logic
    `build_item_aspects()` uses at actual listing time -- without this, the
    eligibility check here could block (or wrongly allow) a candidate
    `build_item_aspects()` would actually decide differently, since the two
    would be using different logic. Defaults to `""` (safe no-op, falls back
    to the original Brand/generic-only behavior) for any caller not yet
    passing it.

    - `match_score` (0-100): eBay's Taxonomy API returns no confidence score
      of its own, so this is self-computed. **First version reused
      `judge_match()`'s exact overlap+fuzzy-blob formula (branch11_pipeline.py)
      -- caught live 2026-08-28 producing a near-0 score for the flag item
      against "United States, Country Flags", an obviously excellent match**:
      that formula fits comparing two similarly-shaped, noun-dense titles
      (its original AliExpress use case), not a long, adjective-heavy eBay
      title against a short 2-4 word category name -- exact-token overlap
      caught nothing ("Flag" vs "Flags" never matches as a set), and a
      single fuzzy ratio over the whole blended word-blob diluted the one
      real match ("flag"~"flags") across mostly-irrelevant filler words
      ("Heavy", "Duty", "Luxury", ...). **Redesigned: for each word in the
      category's own (short) name, find its single best fuzzy-match ratio
      anywhere in the title, then average those per-word scores** -- answers
      "does each core category concept actually show up, even loosely, in
      the title," which suits a sparse short-name-vs-long-title comparison
      far better. Uses the leaf category name only, not the full ancestor
      path -- ancestors add mostly generic words (e.g. "Collectibles") that
      would dilute the signal without being wrong. Still a rough,
      explainable signal for "does this category sound like the item," not
      a verified-correct category.
    - `needs_unresolvable_variation` / `unresolvable_aspects`: whether any
      required aspect is BOTH unresolvable via `_resolve_aspect_value()`
      (added 2026-08-29: generic eBay value, or a real value found in
      `ali_text` -- see that function) AND a real eBay product-variation
      dimension (`aspectEnabledForVariations` -- Size, Color, etc.). This
      project has no eBay Variations API support (multi-SKU/multi-value
      listings) -- Travis's 2026-08-28 decision: skip these rather than
      guess a single arbitrary size/color for what's really a multi-variant
      sourced item. `_resolve_aspect_value()`'s text-matching tiers narrow
      how often this actually triggers without weakening the underlying
      "never guess" rule -- they only ever use a real, evidenced value, not
      an arbitrary pick among several genuine variants.
    """
    status, body = _call(env, "GET",
                          f"/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q={urllib.parse.quote(title)}",
                          credential_mode=credential_mode)
    if status != 200:
        return None
    suggestions = (body or {}).get("categorySuggestions", [])
    if not suggestions:
        return None
    top = suggestions[0]
    category_id = top["category"]["categoryId"]
    category_name = top["category"].get("categoryName", "")
    ancestor_names = [a.get("categoryName", "") for a in reversed(top.get("categoryTreeNodeAncestors", []))]
    category_path = " > ".join([*ancestor_names, category_name])

    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    name_words = set(re.findall(r"[a-z0-9]+", category_name.lower()))
    if name_words and title_words:
        per_word_best = [
            max(difflib.SequenceMatcher(None, nw, tw).ratio() for tw in title_words)
            for nw in name_words
        ]
        match_score = round(100 * sum(per_word_best) / len(per_word_best))
    else:
        match_score = 0

    required = get_required_aspects(env, category_id, credential_mode=credential_mode)
    unresolvable = [
        a["name"] for a in required
        if _resolve_aspect_value(a["name"], a["values"], ali_text, dimension_text) is None
    ]
    unresolvable_variation = [
        a["name"] for a in required
        if a["name"] in unresolvable and a.get("enabled_for_variations")
    ]

    return {
        "category_id": category_id,
        "category_name": category_name,
        "category_path": category_path,
        "match_score": match_score,
        "unresolvable_aspects": unresolvable,
        "needs_unresolvable_variation": bool(unresolvable_variation),
        "unresolvable_variation_aspects": unresolvable_variation,
    }


def list_candidate(env: str, candidate: dict, aliexpress_shipping_cost: float,
                    credential_mode: str = "bitwarden", manual_aspects: dict | None = None) -> dict:
    """Orchestrates the full Step 7 flow for one pipeline candidate (one row
    from branch11_candidates_<date>.json, as approved by Travis in chat by SKU)
    -- create the inventory item, pick a shipping policy, suggest a category,
    create the offer, publish it. Returns a dict with every intermediate
    result so a failure at any stage is fully visible, not just the final
    outcome.

    `manual_aspects`: when `build_item_aspects()` can't safely auto-resolve a
    required aspect (e.g. Type/Size with no generic eBay option -- see
    branch11-listing-rules.md's "Required item aspects"), Travis can supply
    the real value himself for one specific SKU (chat-approved, per-item,
    same trust model as approving a SKU for listing at all) -- pass e.g.
    `{"Type": "Keyboard Folio/Case"}` here to skip the auto-build entirely
    for THIS call and use exactly these values. Merged with "Brand":
    "Unbranded" automatically unless Brand is also explicitly overridden.

    `aliexpress_shipping_cost` is required, not defaulted -- shipping-cost
    extraction from the AliExpress product page isn't built yet (see
    branch11-listing-rules.md next steps), so this must be supplied explicitly
    (e.g. checked by hand) until that exists; silently assuming $0 would pick
    the cheapest shipping policy regardless of the real cost, which is wrong.

    `credential_mode`: "bitwarden" (default, interactive, needs BW_SESSION) or
    "local" (unattended auto-listing path only -- see _call()'s docstring).

    On PRODUCTION specifically: uses PRODUCTION_RETURN_POLICY_ID always (per
    Travis's 2026-08-26 decision).
    """
    from ecommerce_listing_mgmt.pipeline import select_shipping_policy  # local import: avoid a hard circular dependency

    sku = candidate["sku"]
    ebay_item = candidate["ebay_item"]
    ali_match = candidate.get("best_ali_match") or {}
    result: dict = {"sku": sku, "steps": {}}

    policy = select_shipping_policy(env, aliexpress_shipping_cost, credential_mode=credential_mode)
    result["steps"]["shipping_policy"] = policy
    if policy is None:
        result["error"] = f"No shipping policy covers estimated cost ${aliexpress_shipping_cost:.2f} -- flag for manual review"
        return result

    category_id = suggest_category(env, ebay_item["title"], credential_mode=credential_mode)
    result["steps"]["category_id"] = category_id
    if category_id is None:
        result["error"] = "Could not suggest a category -- flag for manual review, do not guess one"
        return result

    if not ali_match.get("title"):
        result["error"] = "No AliExpress match title available -- cannot build an honest listing title, flag for manual review"
        return result
    listing_title = ebay_listing_title(ali_match["title"])

    if not ali_match.get("description"):
        result["error"] = "No AliExpress match description available -- cannot build an honest listing description, flag for manual review"
        return result
    listing_description = scrub_description(ali_match["description"])

    # Required item-specific aspects (Brand/Type/Size/etc.) -- omitting these
    # for a category that requires them creates a real, unpublished, dangling
    # offer that then fails at publish_offer with a confusing error (found
    # 2026-08-28: 4 of 5 auto-listed candidates hit this). Resolve BEFORE
    # creating any real state, not after, so an unresolvable candidate is
    # cleanly skipped instead of leaving orphaned inventory items/offers.
    if manual_aspects:
        aspects = {"Brand": ["Unbranded"], **{k: [v] if isinstance(v, str) else v for k, v in manual_aspects.items()}}
    else:
        aspects = build_item_aspects(env, category_id, f"{listing_title} {listing_description}",
                                      credential_mode=credential_mode, dimension_text=ali_match.get("dimension_text") or "")
    result["steps"]["aspects"] = aspects
    if aspects is None:
        result["error"] = ("Could not confidently resolve a required item aspect "
                            "(Brand/Type/Size/etc.) for this category -- flag for manual review, do not guess")
        return result

    image_url = ali_match.get("imageUrl") or "https://i.ebayimg.com/images/g/XyEAAOSwDkZn83iv/s-l400.jpg"
    status, body = create_inventory_item(
        env, sku=sku, title=listing_title,
        description=listing_description,
        image_url=image_url,
        aspects=aspects,
        credential_mode=credential_mode,
    )
    result["steps"]["create_inventory_item"] = {"status": status, "body": body}
    if status not in (200, 201, 204):
        result["error"] = f"create_inventory_item failed: {_error_detail(status, body)}"
        return result

    policies_kwargs: dict = {
        "fulfillment_policy_id": policy["fulfillmentPolicyId"],
        "payment_policy_id": None,  # filled in below
    }
    status, body = _call(env, "GET", "/sell/account/v1/payment_policy?marketplace_id=EBAY_US", credential_mode=credential_mode)
    payment_policies = body.get("paymentPolicies", []) if status == 200 else []
    if not payment_policies:
        result["error"] = "No payment policy found on this account -- flag for manual review"
        return result
    policies_kwargs["payment_policy_id"] = payment_policies[0]["paymentPolicyId"]

    return_policy_id = PRODUCTION_RETURN_POLICY_ID if env == "production" else None

    status, body = create_offer(
        env, sku=sku, price=ebay_item["priceNum"], category_id=category_id,
        fulfillment_policy_id=policies_kwargs["fulfillment_policy_id"],
        payment_policy_id=policies_kwargs["payment_policy_id"],
        return_policy_id=return_policy_id,
        listing_description=listing_description,
        credential_mode=credential_mode,
    )
    result["steps"]["create_offer"] = {"status": status, "body": body}
    if status not in (200, 201):
        result["error"] = f"create_offer failed: {_error_detail(status, body)}"
        return result
    offer_id = body["offerId"]
    result["offer_id"] = offer_id

    status, body = publish_offer(env, offer_id, credential_mode=credential_mode)
    result["steps"]["publish_offer"] = {"status": status, "body": body}

    # 2026-09-01 (PRODUCTION-READINESS.md Item 32): errorId 25604
    # ("Availability not found... Please try again") happened exactly once
    # across dozens of real auto-listing attempts (2026-08-30) and never
    # recurred -- confirmed live by checking every candidates file since.
    # eBay's own message text, plus create_inventory_item -> create_offer ->
    # publish_offer above being called back-to-back with zero delay, point
    # to a rare eventual-consistency lag on eBay's backend (the freshly-
    # created inventory item's availability data not yet fully propagated
    # when publish_offer queries it moments later) -- not a systemic block
    # like Item 35's Medical Devices policy turned out to be. One short-
    # delay retry, since eBay's own error text says to try again.
    if status not in (200, 201) and isinstance(body, dict) and any(
        e.get("errorId") == 25604 for e in (body.get("errors") or [])
    ):
        result["steps"]["publish_offer_retry_25604"] = True
        time.sleep(5)
        status, body = publish_offer(env, offer_id, credential_mode=credential_mode)
        result["steps"]["publish_offer"] = {"status": status, "body": body}

    if status not in (200, 201):
        detail = _error_detail(status, body)
        note = (" (known eBay sandbox-only platform bug, see branch11-listing-rules.md)"
                if env == "sandbox" and isinstance(body, dict)
                and any(e.get("errorId") == 25002 for e in body.get("errors", [])) else "")
        result["error"] = f"publish_offer failed: {detail}{note}"
        return result

    result["listing_id"] = body.get("listingId")
    result["success"] = True
    return result


def create_merchant_location(env: str, location_key: str = "default", *, city: str = "San Jose",
                              state: str = "CA", postal_code: str = "95125", country: str = "US",
                              address_line1: str | None = "123 Main St",
                              name: str = "Branch11 Test Location",
                              credential_mode: str = "bitwarden") -> tuple[int, dict | None]:
    """Offers need at least one merchant inventory location to reference.

    Defaults are the sandbox test placeholder used since 2026-08-26 -- pass
    real values for `city`/`state`/`postal_code`/`name` (and set
    `address_line1=None` if only a city-level location is wanted, e.g.
    production, per Travis's 2026-08-27 decision: real shipping is flat-rate
    per AliExpress-sourced cost, not calculated from this location, so a
    precise street address isn't needed) on production -- never reuse this
    sandbox placeholder address for a real account.
    """
    address = {"city": city, "stateOrProvince": state, "postalCode": postal_code, "country": country}
    if address_line1:
        address["addressLine1"] = address_line1
    body = {
        "location": {"address": address},
        "locationTypes": ["WAREHOUSE"],
        "name": name,
        "merchantLocationStatus": "ENABLED",
    }
    return _call(env, "POST", f"/sell/inventory/v1/location/{location_key}", body, credential_mode=credential_mode)
