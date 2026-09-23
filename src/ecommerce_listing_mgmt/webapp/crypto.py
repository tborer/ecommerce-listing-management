"""Credential encryption at rest (Fernet: AES-128-CBC + HMAC-SHA256).
ELM_ENCRYPTION_KEY must be a Fernet key; generate one with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Losing or rotating the key makes stored credentials unreadable -- users
would need to re-enter them."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from ecommerce_listing_mgmt.webapp.config import get_settings


class EncryptionNotConfigured(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise EncryptionNotConfigured("ELM_ENCRYPTION_KEY is not set -- credentials can't be stored")
    return Fernet(key.encode())


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as e:
        raise EncryptionNotConfigured("stored credential can't be decrypted with the current ELM_ENCRYPTION_KEY") from e
