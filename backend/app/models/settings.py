from datetime import datetime

from sqlalchemy import Double, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base, UTCDateTimeType


class SystemSettings(Base):
    """The one row of settings an admin changes at runtime; .env only gives the first values."""

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # always 1
    # A reading at this share of its limit or closer is a warning; past the limit it is an alert.
    warning_percent: Mapped[int] = mapped_column(Integer)
    # The interface language for every user: the stations are shared, so nobody switches it alone.
    language: Mapped[str] = mapped_column(String(5))
    # The plant's IANA time zone, such as America/Ciudad_Juarez: every station shows times in it.
    time_zone: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class MetricThreshold(Base):
    """The alert threshold of one metric, changed by an admin."""

    __tablename__ = "metric_thresholds"

    category: Mapped[str] = mapped_column(String(50), primary_key=True)
    threshold: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
