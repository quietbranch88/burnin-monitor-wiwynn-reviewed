"""Password hashing and JWT handling."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.errors import AuthenticationError

# bcrypt only uses the first 72 bytes; bcrypt 5 rejects longer input.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(encoded, hashed.encode())


@lru_cache
def _dummy_hash() -> str:
    """Checked when the username does not exist, with the same cost as a real hash, so the
    response time does not reveal whether an account exists."""
    return hash_password("dummy-password")


async def hash_password_async(password: str) -> str:
    """bcrypt is CPU-bound; keep it off the event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, hashed: str | None) -> bool:
    if hashed is None:
        # The first call computes the cached dummy hash, itself a bcrypt run: keep it in the thread too.
        await asyncio.to_thread(lambda: verify_password(password, _dummy_hash()))
        return False
    return await asyncio.to_thread(verify_password, password, hashed)


def create_access_token(user_id: int, version: int) -> tuple[str, int]:
    """Return the signed token and its lifetime in seconds. `version` is the user's token version.

    No role in the token: the role is read from the database on every request, so a role
    change applies at once and a stale claim could never be trusted by mistake.
    """
    settings = get_settings()
    lifetime = timedelta(minutes=settings.jwt_expire_minutes)
    now = datetime.now(UTC)
    # PyJWT truncates datetime claims to whole seconds. Keep the available
    # precision so cookie synchronization can order logins within one second.
    payload = {"sub": str(user_id), "ver": version, "iat": now.timestamp(), "exp": now + lifetime}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(lifetime.total_seconds())


# The exact shape of a token this API issues. PyJWT decodes base64 leniently: a valid token with
# characters appended after its signature would still verify, so the shape is checked first.
# It matters beyond tidiness: the Streamlit frontend writes the token into its page script
# (the login cookie), and only a string of this shape may ever reach it.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def decode_access_token(token: str) -> tuple[int, int]:
    """Return the user id and token version in a valid token, or raise AuthenticationError."""
    settings = get_settings()
    if not TOKEN_SHAPE.fullmatch(token):
        raise AuthenticationError("Invalid or expired token")
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "ver", "exp"]},
        )
        return int(payload["sub"]), int(payload["ver"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:  # TypeError: e.g. "ver": null
        raise AuthenticationError("Invalid or expired token") from exc
