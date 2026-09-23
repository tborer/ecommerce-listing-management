from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ecommerce_listing_mgmt.webapp.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    oauth_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Credential(Base):
    """One row per (user, provider). `secret_enc` is Fernet-encrypted JSON
    (API keys, OAuth refresh/access tokens); `meta` is non-secret display
    info only (key hint, connected-at, last error)."""
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # "cj" | "ebay"
    secret_enc: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))           # "manual" | "schedule"
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | error
    phase: Mapped[str] = mapped_column(String(20), default="discover")  # discover | match | list | done
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Candidate(Base):
    """An eBay item found by discovery, its best CJ match, and where it is
    in the review/listing flow.

    status: discovered -> no_match | rejected | passed
            passed -> listing_queued -> listed | list_failed
            any -> dismissed
    """
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("user_id", "ebay_item_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="discovered", index=True)
    source: Mapped[str] = mapped_column(String(300), default="")

    ebay_item_id: Mapped[str] = mapped_column(String(40))
    ebay_title: Mapped[str] = mapped_column(String(300))
    ebay_price: Mapped[float] = mapped_column(Float)
    ebay_url: Mapped[str] = mapped_column(String(300))
    ebay_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebay_category_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ebay_discount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    cj_pid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cj_vid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cj_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cj_variant_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cj_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cj_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cj_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_days_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)

    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    criteria: Mapped[list] = mapped_column(JSON, default=list)   # [{rule, ok, detail}]
    passes: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)    # CJ description, freight options, ...

    auto_listed: Mapped[bool] = mapped_column(Boolean, default=False)
    ebay_sku: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ebay_offer_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ebay_listing_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
