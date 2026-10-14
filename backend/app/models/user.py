import enum
from datetime import datetime

from sqlalchemy import Boolean, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base, UTCDateTimeType


class Role(str, enum.Enum):
    admin = "admin"
    user = "user"
    viewer = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda roles: [r.value for r in roles]),
        default=Role.viewer,  # least privilege, as for self-registration
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Part of every token; a logout increments it, so all of this user's tokens stop working.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTimeType, default=utcnow, onupdate=utcnow)
