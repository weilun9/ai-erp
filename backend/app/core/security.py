import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

TokenType = Literal["access", "refresh"]

_hasher = PasswordHash.recommended()  # argon2id

# Used so that "unknown user" and "wrong password" take the same time.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False
    return _hasher.verify(password, password_hash)


def create_token(user_id: uuid.UUID, token_type: TokenType) -> tuple[str, int]:
    """Return (jwt, lifetime_seconds)."""
    s = get_settings()
    lifetime = (
        timedelta(minutes=s.access_token_minutes)
        if token_type == "access"
        else timedelta(days=s.refresh_token_days)
    )
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "typ": token_type,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), int(
        lifetime.total_seconds()
    )


def decode_token(token: str, expected_type: TokenType) -> uuid.UUID | None:
    """Return the user id, or None if the token is invalid, expired or of the wrong type."""
    s = get_settings()
    try:
        payload = jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub"]}
        )
        if payload.get("typ") != expected_type:
            return None
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        return None
