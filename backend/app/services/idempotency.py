"""Run a create or import at most once per `Idempotency-Key`.

A client that lost the response to a request it sent can send it again with the same key
and get the stored response, instead of creating the records twice. The key row is
written in the same transaction as the records, so either both exist or neither does.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.db.session import error_code
from app.models import IdempotencyKey, User

# Keys are kept this long; a retry after that creates the records again.
RETENTION_HOURS = 24
# Another request inserted the same key first: duplicate key, or under MariaDB's snapshot
# isolation "record has changed since last read".
KEY_TAKEN_CODES = frozenset({1062, 1020})


def fingerprint(operation: str, payload: object) -> str:
    """Hash of what the request asks for, to refuse a key reused for something else."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(operation.encode() + b"\0" + body).hexdigest()


def _stored(row: IdempotencyKey, request_hash: str) -> dict:
    if row.request_hash != request_hash:
        raise ConflictError("This Idempotency-Key was already used for a different request")
    return row.response


async def run_once(
    session: AsyncSession,
    user: User,
    key: str,
    request_hash: str,
    work: Callable[[], Awaitable[dict]],
) -> tuple[dict, bool]:
    """Return (response, replayed). `work` stages the changes and returns the response body."""
    user_id = user.id  # read now: a rollback below expires every object in the session
    existing = await session.get(IdempotencyKey, (user_id, key))
    if existing is not None:
        return _stored(existing, request_hash), True
    body = await work()
    session.add(IdempotencyKey(user_id=user_id, key=key, request_hash=request_hash, response=body))
    try:
        await session.commit()
    except (IntegrityError, OperationalError) as exc:
        if error_code(exc) not in KEY_TAKEN_CODES:
            raise
        # The same key arrived twice at once and the other request committed first: this
        # transaction, records included, is rolled back and the stored result returned.
        await session.rollback()
        existing = await session.get(IdempotencyKey, (user_id, key))
        if existing is None:
            raise ConflictError(
                "A request with this Idempotency-Key is still being processed, retry shortly", retry_after=1
            ) from exc
        return _stored(existing, request_hash), True
    return body, False
