import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.time import to_storage
from app.db.session import engine
from app.models import RealtimeAlert, RealtimeMinuteStat, RealtimeReading, Record, SystemLog, User
from app.schemas.admin import DatabaseStatus, PoolStatus, WriterStatus
from app.services.batch import BatchWriter
from app.services.realtime import RealtimeHub

REQUEST_LOGGER = "app.request"  # the request middleware's lines; everything else is an event


@dataclass(frozen=True)
class LogFilters:
    level: str | None = None
    path: str | None = None
    status_code: int | None = None
    user_id: int | None = None
    q: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    logger: str | None = None
    request_id: str | None = None
    # "events": changes, logins and other events; "requests": one line per API request.
    kind: str | None = None


async def list_logs(session: AsyncSession, f: LogFilters, page: int, page_size: int) -> tuple[list[SystemLog], int]:
    conditions = []
    if f.level:
        conditions.append(SystemLog.level == f.level.upper())
    if f.path:
        conditions.append(SystemLog.path.contains(f.path, autoescape=True))
    if f.status_code is not None:
        conditions.append(SystemLog.status_code == f.status_code)
    if f.user_id is not None:
        conditions.append(SystemLog.user_id == f.user_id)
    if f.q:
        conditions.append(SystemLog.message.contains(f.q, autoescape=True))
    if f.logger:
        conditions.append(SystemLog.logger.startswith(f.logger, autoescape=True))  # "app" matches "app.users"
    if f.request_id:
        conditions.append(SystemLog.request_id == f.request_id)
    if f.kind == "events":
        conditions.append(SystemLog.logger != REQUEST_LOGGER)
    elif f.kind == "requests":
        conditions.append(SystemLog.logger == REQUEST_LOGGER)
    if f.start is not None:
        conditions.append(SystemLog.created_at >= to_storage(f.start))
    if f.end is not None:
        conditions.append(SystemLog.created_at <= to_storage(f.end))
    total = await session.scalar(select(func.count()).select_from(SystemLog).where(*conditions))
    query = (
        select(SystemLog)
        .where(*conditions)
        .order_by(SystemLog.created_at.desc(), SystemLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(await session.scalars(query)), total or 0


async def usernames(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """Usernames for the user ids on a page of logs, so admins see names instead of numbers."""
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.username).where(User.id.in_(ids)))
    return {row.id: row.username for row in rows}


async def list_history(
    session: AsyncSession,
    *,
    category: str | None,
    server: str | None,
    sensor: str | None,
    anomalies_only: bool,
    start: datetime | None,
    end: datetime | None,
    before_at: datetime | None,
    before_id: int | None,
    limit: int,
) -> tuple[list[RealtimeReading], tuple[datetime, int] | None]:
    """Newest generation time first, ties by id; an exclusive (time, id) cursor gives the next page.

    Ordering by time lets the `generated_at` index serve every page, "up to a time" included:
    ordered by insertion id, that query walked the primary key backwards through every row
    stored after the chosen moment. A reading stored late (after an outage) sorts at its own
    time, so it is on the page of its minute rather than on the newest page.
    """
    conditions = []
    if category:
        conditions.append(RealtimeReading.category == category.strip().lower())
    if server:
        conditions.append(RealtimeReading.server == server)
    if sensor:
        conditions.append(RealtimeReading.sensor == sensor)
    if anomalies_only:
        # `= true`, not `IS true`: MariaDB uses an index only for the comparison.
        conditions.append(RealtimeReading.is_anomaly == true())
    if start is not None:
        conditions.append(RealtimeReading.generated_at >= to_storage(start))
    if end is not None:
        conditions.append(RealtimeReading.generated_at <= to_storage(end))
    if before_at is not None and before_id is not None:
        at = to_storage(before_at)
        conditions.append(or_(RealtimeReading.generated_at < at,
                              and_(RealtimeReading.generated_at == at, RealtimeReading.id < before_id)))
    query = (
        select(RealtimeReading)
        .where(*conditions)
        .order_by(RealtimeReading.generated_at.desc(), RealtimeReading.id.desc())
        .limit(limit + 1)
    )
    rows = list(await session.scalars(query))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].generated_at, rows[-1].id) if more else None


def _writer_status(writer: BatchWriter) -> WriterStatus:
    return WriterStatus(
        pending=writer.pending,
        queued=writer.queued,
        in_flight=writer.in_flight,
        written=writer.written,
        dropped=writer.dropped,
        flushes=writer.flushes,
        last_flush_at=writer.last_flush_at,
        last_error=writer.last_error,
        backoff_seconds=writer.backoff_seconds,
    )


async def database_status(
    session: AsyncSession,
    settings: Settings,
    hub: RealtimeHub,
    log_writer: BatchWriter,
) -> DatabaseStatus:
    started = time.perf_counter()
    await session.scalar(select(literal(1)))
    ping_ms = round((time.perf_counter() - started) * 1000, 2)
    connection = await session.connection()
    version_info = connection.dialect.server_version_info or ()
    server_version = ".".join(str(part) for part in version_info)

    tables = {}
    for model in (User, Record, RealtimeMinuteStat, RealtimeAlert):
        tables[model.__tablename__] = await session.scalar(select(func.count()).select_from(model)) or 0
    # High-volume, append-only tables: estimate from the primary-key range instead of
    # scanning every row; retention deletes from the old end, so the range stays close.
    for big in (RealtimeReading, SystemLog):
        low, high = (await session.execute(select(func.min(big.id), func.max(big.id)))).one()
        tables[big.__tablename__] = 0 if high is None else high - low + 1

    pool = engine.pool
    assert isinstance(pool, QueuePool)  # the async engine's AsyncAdaptedQueuePool: the counters below exist
    return DatabaseStatus(
        dialect=f"{engine.dialect.name}+{engine.dialect.driver}",
        server_version=server_version,
        ping_ms=ping_ms,
        pool=PoolStatus(
            size=pool.size(),
            checked_in=pool.checkedin(),
            checked_out=pool.checkedout(),
            # SQLAlchemy reports unused capacity as negative overflow.
            overflow=max(pool.overflow(), 0),
            configured_size=settings.db_pool_size,
            configured_max_overflow=settings.db_max_overflow,
        ),
        table_rows=tables,
        approximate_rows=[RealtimeReading.__tablename__, SystemLog.__tablename__],
        realtime_writer=_writer_status(hub.writer),
        minute_writer=_writer_status(hub.minute_writer),
        alert_writer=_writer_status(hub.alert_writer),
        log_writer=_writer_status(log_writer),
        skipped_ticks=hub.skipped_ticks,
        websocket_clients=hub.broadcaster.subscribers,
        generator_running=hub.generating,
    )
