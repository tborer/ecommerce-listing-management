from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from ecommerce_listing_mgmt.webapp.config import get_settings


class Base(DeclarativeBase):
    pass


_engines: dict[str, Engine] = {}
_initialized: set[str] = set()


def get_engine() -> Engine:
    url = get_settings().database_url
    engine = _engines.get(url)
    if engine is None:
        if url.startswith("sqlite"):
            engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            # Serverless: no pooled connections held across invocations
            # (Neon's pooled endpoint does the pooling).
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        _engines[url] = engine
    if url not in _initialized:
        from ecommerce_listing_mgmt.webapp import (
            models,  # noqa: F401  (registers tables)
        )
        Base.metadata.create_all(engine)
        _initialized.add(url)
    return engine


def get_db() -> Iterator[Session]:
    session = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def new_session() -> Session:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)()
