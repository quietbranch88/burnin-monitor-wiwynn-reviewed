from datetime import datetime

from sqlalchemy import Double, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utcnow
from app.db.base import Base, UTCDateTimeType
from app.models.user import User


class Record(Base):
    """A test or inspection record entered or imported by an engineer."""

    __tablename__ = "records"
    __table_args__ = (Index("ix_records_category_recorded_at", "category", "recorded_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # DOUBLE: MariaDB's FLOAT keeps only about 6 significant digits.
    value: Mapped[float] = mapped_column(Double)
    category: Mapped[str] = mapped_column(String(50))
    # The server under test, as in the live telemetry (e.g. S0017).
    server: Mapped[str] = mapped_column(String(20), index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTimeType, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow, onupdate=utcnow)

    # Many-to-one, always needed for the owner name: load it in the same query.
    owner: Mapped[User] = relationship(lazy="joined", innerjoin=True)

    @property
    def owner_username(self) -> str:
        return self.owner.username
