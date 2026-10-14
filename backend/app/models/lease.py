from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTimeType


class GeneratorLease(Base):
    """Which process runs the telemetry generator against this database.

    One row per lease name. A process holds the lease while it renews `renewed_at`; a second
    worker or instance that starts with the same database sees a live holder and keeps its
    generator off, so two fleets never write under the same server ids.
    """

    __tablename__ = "generator_lease"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    holder: Mapped[str] = mapped_column(String(80))
    renewed_at: Mapped[datetime] = mapped_column(UTCDateTimeType)
