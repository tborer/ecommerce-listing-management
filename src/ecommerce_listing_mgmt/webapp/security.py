"""Passwords (bcrypt) and cookie sessions (random token, only its SHA-256
stored server-side)."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ecommerce_listing_mgmt.webapp.config import get_settings
from ecommerce_listing_mgmt.webapp.db import get_db
from ecommerce_listing_mgmt.webapp.models import User, UserSession

SESSION_COOKIE = "elm_session"
SESSION_DAYS = 30


def _prehash(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes; prehash so long passwords count in full.
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode())
    except ValueError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, response: Response) -> UserSession:
    token = secrets.token_urlsafe(32)
    sess = UserSession(user_id=user.id, token_hash=token_hash(token),
                       expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))
    db.add(sess)
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=get_settings().secure_cookies, samesite="lax", path="/")
    return sess


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_session(request: Request, db: Session = Depends(get_db)) -> UserSession:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not logged in")
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    if not sess or _aware(sess.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(401, "session expired")
    return sess


def current_user(sess: UserSession = Depends(current_session), db: Session = Depends(get_db)) -> User:
    user = db.get(User, sess.user_id)
    if not user:
        raise HTTPException(401, "not logged in")
    return user
