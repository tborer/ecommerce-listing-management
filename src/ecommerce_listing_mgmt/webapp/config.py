"""Runtime configuration, read from environment variables on every call so
tests (and Vercel env changes) take effect without module reloads."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def normalize_database_url(url: str) -> str:
    """Neon/Vercel hand out postgres:// or postgresql:// URLs; SQLAlchemy
    needs the psycopg (v3) driver named explicitly."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_key: str | None
    allow_signup: bool
    cron_secret: str | None
    ebay_env: str
    app_base_url: str
    run_budget_seconds: float
    secure_cookies: bool
    auto_list_globally_disabled: bool
    on_vercel: bool


def get_settings() -> Settings:
    on_vercel = bool(os.environ.get("VERCEL"))
    raw_db = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    if not raw_db:
        if on_vercel:
            raise RuntimeError("DATABASE_URL is not set -- add a Neon database to the Vercel project")
        raw_db = "sqlite:///./elm-dev.db"
    return Settings(
        database_url=normalize_database_url(raw_db),
        encryption_key=os.environ.get("ELM_ENCRYPTION_KEY") or None,
        allow_signup=_bool("ALLOW_SIGNUP", False),
        cron_secret=os.environ.get("CRON_SECRET") or None,
        ebay_env=os.environ.get("EBAY_ENV", "production"),
        app_base_url=(os.environ.get("APP_BASE_URL") or "http://localhost:3000").rstrip("/"),
        run_budget_seconds=float(os.environ.get("ELM_RUN_BUDGET_SECONDS", "45")),
        secure_cookies=_bool("SECURE_COOKIES", on_vercel),
        auto_list_globally_disabled=_bool("AUTO_LIST_GLOBALLY_DISABLED", False),
        on_vercel=on_vercel,
    )
