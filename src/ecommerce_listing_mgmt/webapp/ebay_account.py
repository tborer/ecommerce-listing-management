"""Per-user eBay connection: OAuth consent + token refresh (app keys from
env, each user's refresh token encrypted in the DB), business policies, and
publishing a matched candidate as a listing via ebay/listing.py helpers
(credential_mode="context")."""
from __future__ import annotations

import html
import re
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from ecommerce_listing_mgmt.ebay import listing as ebay_listing
from ecommerce_listing_mgmt.ebay.auth import (
    _ENDPOINTS,
    _post_token_request,
    load_env_credentials,
)
from ecommerce_listing_mgmt.webapp.config import get_settings
from ecommerce_listing_mgmt.webapp.crypto import decrypt_json, encrypt_json
from ecommerce_listing_mgmt.webapp.models import Candidate, Credential, User, utcnow
from ecommerce_listing_mgmt.webapp.schemas import UserSettingsModel

# api_scope: Taxonomy (category aspects); sell.inventory: create/publish
# listings; sell.account.readonly: read the seller's business policies.
USER_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
]


class EbayNotConnected(RuntimeError):
    pass


class ListingError(RuntimeError):
    pass


def app_configured() -> bool:
    try:
        creds = load_env_credentials(get_settings().ebay_env)
    except RuntimeError:
        return False
    return bool(creds.runame)


def consent_url(state: str) -> str:
    env = get_settings().ebay_env
    creds = load_env_credentials(env)
    if not creds.runame:
        raise RuntimeError("EBAY_RUNAME is not set")
    params = {"client_id": creds.app_id, "redirect_uri": creds.runame, "response_type": "code",
              "scope": " ".join(USER_SCOPES), "state": state}
    return f"{_ENDPOINTS[env]['auth_base']}?{urllib.parse.urlencode(params)}"


def _token_request(params: dict) -> dict:
    env = get_settings().ebay_env
    return _post_token_request(_ENDPOINTS[env], load_env_credentials(env), params)


def _get_cred(db: Session, user: User) -> Credential | None:
    return db.scalar(select(Credential).where(Credential.user_id == user.id, Credential.provider == "ebay"))


def exchange_code_and_store(db: Session, user: User, code: str) -> None:
    env = get_settings().ebay_env
    result = _token_request({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": load_env_credentials(env).runame})
    secret = {
        "env": env,
        "refresh_token": result["refresh_token"],
        "refresh_expires_at": time.time() + float(result.get("refresh_token_expires_in") or 18 * 30 * 86400),
        "access_token": result.get("access_token"),
        "access_expires_at": time.time() + float(result.get("expires_in") or 7200) - 120,
    }
    cred = _get_cred(db, user) or Credential(user_id=user.id, provider="ebay")
    cred.secret_enc = encrypt_json(secret)
    cred.meta = {"env": env, "connected_at": utcnow().isoformat(), "scopes": USER_SCOPES}
    db.add(cred)
    db.commit()


def connection_status(db: Session, user: User) -> dict:
    cred = _get_cred(db, user)
    if not cred:
        return {"connected": False, "app_configured": app_configured(), "env": get_settings().ebay_env}
    return {"connected": True, "app_configured": app_configured(), **(cred.meta or {})}


def disconnect(db: Session, user: User) -> None:
    cred = _get_cred(db, user)
    if cred:
        db.delete(cred)
        db.commit()


def user_access_token(db: Session, user: User) -> str:
    cred = _get_cred(db, user)
    if not cred:
        raise EbayNotConnected("connect your eBay account first")
    secret = decrypt_json(cred.secret_enc)
    if secret.get("access_token") and float(secret.get("access_expires_at") or 0) > time.time():
        return secret["access_token"]
    result = _token_request({"grant_type": "refresh_token", "refresh_token": secret["refresh_token"],
                             "scope": " ".join(USER_SCOPES)})
    secret["access_token"] = result["access_token"]
    secret["access_expires_at"] = time.time() + float(result.get("expires_in") or 7200) - 120
    cred.secret_enc = encrypt_json(secret)
    db.commit()
    return secret["access_token"]


@contextmanager
def as_user(db: Session, user: User) -> Iterator[str]:
    """Run ebay/listing.py helpers (credential_mode="context") as this user."""
    token = user_access_token(db, user)
    reset = ebay_listing.USER_ACCESS_TOKEN.set(token)
    try:
        yield get_settings().ebay_env
    finally:
        ebay_listing.USER_ACCESS_TOKEN.reset(reset)


def get_policies(db: Session, user: User) -> dict:
    out: dict[str, list[dict]] = {}
    with as_user(db, user) as env:
        for kind in ("fulfillment", "payment", "return"):
            status, body = ebay_listing._call(
                env, "GET", f"/sell/account/v1/{kind}_policy?marketplace_id=EBAY_US", credential_mode="context")
            if status != 200:
                raise ListingError(f"couldn't read {kind} policies: {ebay_listing._error_detail(status, body)} "
                                   "(is the account opted in to business policies?)")
            key = f"{kind}Policies"
            out[kind] = [{"id": p.get(f"{kind}PolicyId"), "name": p.get("name")} for p in (body or {}).get(key, [])]
    return out


# --- listing a candidate -----------------------------------------------------

_SUPPLIER_TERMS = re.compile(r"\b(cj\s*dropshipping|cjdropshipping|cj)\b", re.IGNORECASE)


def listing_description(cand: Candidate) -> str:
    text = (cand.details or {}).get("cj_description") or cand.cj_title or cand.ebay_title
    text = _SUPPLIER_TERMS.sub("", ebay_listing.scrub_description(text))
    paragraphs = [html.escape(p.strip()) for p in text.split("\n") if p.strip()]
    return "<p>" + "</p><p>".join(paragraphs[:40]) + "</p>"


def listing_title(cand: Candidate) -> str:
    base = cand.cj_title or cand.ebay_title
    if cand.cj_variant_name and (cand.details or {}).get("variant_count", 1) > 1:
        base = f"{base} - {cand.cj_variant_name}"
    return ebay_listing.ebay_listing_title(_SUPPLIER_TERMS.sub("", base).strip())


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _ensure_location(env: str, s: UserSettingsModel) -> None:
    status, _ = ebay_listing._call(env, "GET", "/sell/inventory/v1/location/default", credential_mode="context")
    if status == 200:
        return
    if not (s.location_city and s.location_state and s.location_postal_code):
        raise ListingError("set your item location (city, state, ZIP) in Settings before listing")
    status, body = ebay_listing.create_merchant_location(
        env, "default", city=s.location_city, state=s.location_state, postal_code=s.location_postal_code,
        country=s.location_country, address_line1=None, name="Primary location", credential_mode="context")
    if not _ok(status):
        raise ListingError(f"couldn't create the eBay inventory location: {ebay_listing._error_detail(status, body)}")


def _existing_offer_id(env: str, sku: str) -> str | None:
    status, body = ebay_listing._call(env, "GET", f"/sell/inventory/v1/offer?sku={urllib.parse.quote(sku)}",
                                      credential_mode="context")
    offers = (body or {}).get("offers") or [] if status == 200 else []
    return offers[0].get("offerId") if offers else None


def publish_candidate(db: Session, user: User, cand: Candidate, s: UserSettingsModel) -> str:
    """Create inventory item -> offer -> publish on the user's own eBay
    account. Returns the listing id; raises ListingError with eBay's own
    error text otherwise."""
    if not (s.ebay_fulfillment_policy_id and s.ebay_payment_policy_id and s.ebay_return_policy_id):
        raise ListingError("choose your eBay shipping, payment and return policies in Settings before listing")
    if not cand.cj_image_url and not cand.ebay_image_url:
        raise ListingError("no product image available")
    with as_user(db, user) as env:
        _ensure_location(env, s)
        category_id = cand.ebay_category_id or ebay_listing.suggest_category(
            env, cand.cj_title or cand.ebay_title, credential_mode="context")
        if not category_id:
            raise ListingError("couldn't determine an eBay category")
        title_text = f"{cand.cj_title or ''} {cand.cj_variant_name or ''} {(cand.details or {}).get('cj_description', '')}"
        aspects = ebay_listing.build_item_aspects(env, category_id, title_text, credential_mode="context")
        if aspects is None:
            required = [a["name"] for a in ebay_listing.get_required_aspects(env, category_id, credential_mode="context")]
            raise ListingError(f"eBay category {category_id} requires item specifics that couldn't be filled "
                               f"automatically ({', '.join(required)})")
        sku = cand.ebay_sku or f"elm-{cand.id}"
        cand.ebay_sku = sku
        description = listing_description(cand)
        status, body = ebay_listing.create_inventory_item(
            env, sku, listing_title(cand), description, cand.cj_image_url or cand.ebay_image_url,
            quantity=1, aspects=aspects, credential_mode="context")
        if not _ok(status):
            raise ListingError(f"create inventory item failed: {ebay_listing._error_detail(status, body)}")
        offer_id = cand.ebay_offer_id or _existing_offer_id(env, sku)
        if not offer_id:
            status, body = ebay_listing.create_offer(
                env, sku, cand.list_price or cand.ebay_price, category_id,
                s.ebay_fulfillment_policy_id, s.ebay_payment_policy_id, s.ebay_return_policy_id,
                listing_description=description, credential_mode="context")
            if not _ok(status):
                raise ListingError(f"create offer failed: {ebay_listing._error_detail(status, body)}")
            offer_id = (body or {}).get("offerId")
        cand.ebay_offer_id = offer_id
        db.commit()
        status, body = ebay_listing.publish_offer(env, offer_id, credential_mode="context")
        if not _ok(status):
            raise ListingError(f"publish failed: {ebay_listing._error_detail(status, body)}")
        return str((body or {}).get("listingId") or "")
