"""Declarative base shared by every ORM model and by Alembic."""

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase

# Stable constraint names keep Alembic migrations deterministic. MariaDB always
# names the primary key PRIMARY, so no "pk" entry.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

# Naive UTC with microseconds on MariaDB.
UTCDateTimeType = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
