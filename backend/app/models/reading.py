from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, Double, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base, UTCDateTimeType


class RealtimeReading(Base):
    """A persisted telemetry reading from one sensor of one server; written in batches."""

    __tablename__ = "realtime_readings"
    __table_args__ = (
        Index("ix_realtime_readings_category_generated_at", "category", "generated_at"),
        # One reading per server, time and sensor: a retried batch cannot store a row twice.
        # Its leading columns also serve the server and time history queries.
        Index(
            "ux_realtime_readings_server_generated_at_category_sensor",
            "server", "generated_at", "category", "sensor", unique=True,
        ),
        # InnoDB adds the primary key to every index, so this is (is_anomaly, generated_at, id):
        # the alert history, newest time first, reads it backwards instead of scanning the table.
        Index("ix_realtime_readings_is_anomaly_generated_at", "is_anomaly", "generated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(50))
    # One of the server's sensors in that category, e.g. GPU3 in gpu_temp.
    sensor: Mapped[str] = mapped_column(String(20))
    value: Mapped[float] = mapped_column(Double)
    threshold: Mapped[float] = mapped_column(Double)
    is_anomaly: Mapped[bool] = mapped_column(Boolean)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, index=True)
    persisted_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow)


class RealtimeMinuteStat(Base):
    """Per-minute, per-category aggregate of all readings; analytics read this, not raw rows."""

    __tablename__ = "realtime_minute_stats"

    minute: Mapped[datetime] = mapped_column(UTCDateTimeType, primary_key=True)
    category: Mapped[str] = mapped_column(String(50), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    total: Mapped[float] = mapped_column(Double)
    minimum: Mapped[float] = mapped_column(Double)
    maximum: Mapped[float] = mapped_column(Double)
    anomalies: Mapped[int] = mapped_column(Integer)
    # Receipts live as long as their aggregate; raw-data backfill can create a bucket without them.
    applied_contributions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
