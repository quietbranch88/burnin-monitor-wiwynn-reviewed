from datetime import datetime

from sqlalchemy import BigInteger, Double, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTimeType


class RealtimeAlert(Base):
    """One alert of one sensor: from its first reading past the limit to its first reading back inside.

    Raw readings are kept for hours; a burn-in run lasts days. These events are what a run can
    be asked afterwards: did S0017 overheat on its first night, which sensor, how far, how long.
    Written by the generator when the alert ends (or, without an end, when the API stops).
    """

    __tablename__ = "realtime_alerts"
    __table_args__ = (
        # One event per sensor and start: a retried batch cannot store an event twice. Its
        # leading columns serve "this server's alerts" in time order.
        Index("ux_realtime_alerts_server_category_sensor_started_at", "server", "category", "sensor", "started_at", unique=True),
        Index("ix_realtime_alerts_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(50))
    sensor: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    threshold: Mapped[float] = mapped_column(Double)
    peak_value: Mapped[float] = mapped_column(Double)  # the reading furthest past the limit
    readings: Mapped[int] = mapped_column(Integer)  # readings past the limit, one per tick
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTimeType, nullable=True)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
