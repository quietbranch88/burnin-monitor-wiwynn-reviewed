from datetime import datetime

from sqlalchemy import BigInteger, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTimeType


class SystemLog(Base):
    """Request logs and application events. No foreign key: logs outlive users."""

    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, index=True)
    level: Mapped[str] = mapped_column(String(10), index=True)
    logger: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(36))
    method: Mapped[str | None] = mapped_column(String(10))
    path: Mapped[str | None] = mapped_column(String(500))
    status_code: Mapped[int | None] = mapped_column(Integer, index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45))
