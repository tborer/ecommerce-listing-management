from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ecommerce_listing_mgmt.ebay.browse import DEALS_PAGE_CATEGORIES

DEFAULT_DEAL_CATEGORIES = [
    "https://www.ebay.com/deals/home-garden/pet-supplies",
    "https://www.ebay.com/deals/home-garden/kitchen-dining-bar",
    "https://www.ebay.com/deals/trending/home-garden/home-improvement",
]


def deal_category_label(url: str) -> str:
    """https://www.ebay.com/deals/home-garden/kitchen-dining-bar -> "Kitchen Dining Bar"."""
    return url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()


class UserSettingsModel(BaseModel):
    """Per-user discovery criteria, schedule and listing options. Stored as
    JSON; unknown keys dropped, missing keys defaulted, so adding a field
    never needs a migration."""
    model_config = {"extra": "ignore"}

    # Discovery
    deal_categories: list[str] = Field(default_factory=lambda: list(DEFAULT_DEAL_CATEGORIES))
    keywords: list[str] = Field(default_factory=list, max_length=25)
    items_per_run: int = Field(25, ge=1, le=200)
    min_ebay_price: float = Field(10.0, ge=0)
    max_ebay_price: float = Field(100.0, gt=0)

    # Criteria
    fee_pct: float = Field(17.0, ge=0, le=60)
    target_margin_pct: float = Field(16.0, ge=0, le=90)
    min_profit: float = Field(3.0, ge=0)
    max_shipping_cost: float = Field(15.0, ge=0)
    max_delivery_days: int = Field(15, ge=1, le=90)
    min_match_score: int = Field(50, ge=0, le=100)
    cj_warehouse: Literal["any", "US"] = "any"
    price_undercut_pct: float = Field(0.0, ge=0, le=50)

    # Schedule (Vercel cron ticks; see docs/deploy.md)
    schedule_enabled: bool = False
    schedule_hour_utc: int = Field(14, ge=0, le=23)

    # Listing
    auto_list_enabled: bool = False
    auto_list_max_per_run: int = Field(3, ge=0, le=25)
    ebay_fulfillment_policy_id: str | None = None
    ebay_payment_policy_id: str | None = None
    ebay_return_policy_id: str | None = None
    location_city: str | None = None
    location_state: str | None = None
    location_postal_code: str | None = None
    location_country: str = "US"

    @field_validator("deal_categories")
    @classmethod
    def _known_categories(cls, v: list[str]) -> list[str]:
        unknown = [u for u in v if u not in DEALS_PAGE_CATEGORIES]
        if unknown:
            raise ValueError(f"unknown deal categories: {unknown}")
        return list(dict.fromkeys(v))

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(k.strip() for k in v if k and k.strip()))

    @model_validator(mode="after")
    def _ranges(self) -> UserSettingsModel:
        if self.min_ebay_price >= self.max_ebay_price:
            raise ValueError("min_ebay_price must be below max_ebay_price")
        if self.fee_pct + self.target_margin_pct >= 100:
            raise ValueError("fee_pct + target_margin_pct must be under 100")
        return self


class AuthIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("enter a valid email address")
        return v


class CJCredentialIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)
