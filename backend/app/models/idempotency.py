from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base, UTCDateTimeType


class IdempotencyKey(Base):
    """The stored outcome of a create or import sent with an `Idempotency-Key` header.

    Written in the same transaction as the records it created, so a key exists exactly
    when its records do. A retry with the same key gets the stored response back.
    """

    __tablename__ = "idempotency_keys"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Binary collation: keys are compared exactly, "AbC" and "abc" are different keys.
    key: Mapped[str] = mapped_column(String(100, collation="utf8mb4_bin"), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow, index=True)
