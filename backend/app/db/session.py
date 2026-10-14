"""Async engine, connection pool and session factory."""

from collections.abc import AsyncIterator

from sqlalchemy.exc import DataError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    # Test each connection on checkout so a restarted database does not
    # surface as a request error.
    pool_pre_ping=True,
    # A connection attempt that gets no answer (a black-holed host) fails in seconds instead
    # of holding a batch write, and its chunk's share of the pool, for the OS default.
    connect_args={"connect_timeout": 5},
    # Error messages must not carry bound values (usernames, emails) into the logs.
    hide_parameters=True,
    echo=settings.db_echo,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# The driver reports connection loss, lock timeouts and plain SQL mistakes alike as
# OperationalError, so the MariaDB error code decides what kind of failure it is.
DATABASE_ERRORS = (OperationalError, InterfaceError, PoolTimeout)
# Cannot connect, connection lost, too many connections, server shutting down.
CONNECTION_ERROR_CODES = frozenset({1040, 1053, 2002, 2003, 2005, 2006, 2013, 2055})
# A unique or primary key already holds the value.
DUPLICATE_KEY = 1062
# Another transaction changed the row first (1020, snapshot isolation), deadlock, lock wait timeout.
CONFLICT_ERROR_CODES = frozenset({1020, 1205, 1213})


def error_code(exc: Exception) -> int | None:
    """The MariaDB error number behind a SQLAlchemy error, if there is one."""
    args = getattr(getattr(exc, "orig", None), "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def is_unavailable(exc: Exception) -> bool:
    """The database could not be reached; the same request can succeed later."""
    if isinstance(exc, PoolTimeout | InterfaceError) or getattr(exc, "connection_invalidated", False):
        return True
    return isinstance(exc, OperationalError) and error_code(exc) in CONNECTION_ERROR_CODES


def is_conflict(exc: Exception) -> bool:
    """Another transaction got there first; retrying the request can succeed."""
    return isinstance(exc, OperationalError) and error_code(exc) in CONFLICT_ERROR_CODES


def is_rejected_row(exc: Exception) -> bool:
    """The database refused the data itself; retrying the same row cannot succeed."""
    return isinstance(exc, (DataError, IntegrityError))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
