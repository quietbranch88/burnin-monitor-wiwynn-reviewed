"""Purge raw readings and system logs past their retention, in small chunks."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import CursorResult, delete, select, tuple_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import InstrumentedAttribute

from app.core.config import Settings
from app.core.time import utcnow
from app.db.base import Base
from app.db.session import SessionLocal, is_conflict
from app.models import IdempotencyKey, RealtimeAlert, RealtimeReading, SystemLog
from app.services.alerts import RETENTION_DAYS as ALERT_RETENTION_DAYS
from app.services.idempotency import RETENTION_HOURS as IDEMPOTENCY_HOURS

logger = logging.getLogger("app.retention")

CHUNK = 20_000
# Write conflicts with other transactions tolerated per purge before giving up until the next run.
MAX_CONFLICT_RETRIES = 10
INTERVAL_SECONDS = 60
# How long one purge waits for the writer to catch up before deleting anyway: a writer that
# is behind for this long is overloaded, and an ever-growing table would fill the disk.
MAX_BUSY_WAIT_SECONDS = 600
NEVER_BUSY: Callable[[], bool] = lambda: False  # noqa: E731


async def purge_before(
    model: type[Base], column: InstrumentedAttribute, cutoff: datetime, chunk: int = CHUNK, busy: Callable[[], bool] = NEVER_BUSY
) -> int:
    """Delete rows older than `cutoff`, at most `chunk` rows per transaction to keep locks short.

    With a single increasing id, each round reads the `chunk` lowest ids among the expired
    rows, walking the primary key, then deletes "expired and id up to the largest of them":
    a primary key range holding exactly those rows. The next round starts after that range:
    starting again from the lowest id would walk over every row deleted so far, which InnoDB
    keeps until its purge thread removes them. Ordering by time instead would let a reading
    stored late (after an outage) stretch the range over the whole backlog, and a delete by
    time alone was planned as a full table scan once most rows had expired.

    Before each chunk the purge waits while `busy()` is true: storing new readings comes
    before deleting old ones, so under load the table grows instead of readings being dropped.
    The wait has a ceiling, `MAX_BUSY_WAIT_SECONDS` per purge: past it the purge goes on
    anyway, because a table that only grows ends with a full disk and no database at all.
    """
    key = model.__mapper__.primary_key
    single = len(key) == 1
    removed, conflicts, after = 0, 0, None
    waited = 0
    while True:
        while busy() and waited < MAX_BUSY_WAIT_SECONDS:
            await asyncio.sleep(1)
            waited += 1
            if waited == MAX_BUSY_WAIT_SECONDS:
                logger.warning("%s purge waited %d s for the writer to catch up; deleting anyway",
                               model.__tablename__, waited)
        async with SessionLocal() as session:
            expired = column < cutoff
            if single and after is not None:
                expired = expired & (key[0] > after)
            order = key[0] if single else column
            rows = (await session.execute(select(*key).where(expired).order_by(order).limit(chunk))).all()
            # End the reading transaction first. Deleting inside it, MariaDB's snapshot
            # isolation refuses rows that changed after the read (error 1020).
            await session.commit()
            if not rows:
                return removed
            if single:
                last = max(row[0] for row in rows)
                match = expired & (key[0] <= last)
            else:
                match = tuple_(*key).in_(rows)
            try:
                result = await session.execute(
                    delete(model).where(match), execution_options={"synchronize_session": False}
                )
                await session.commit()
            except OperationalError as exc:
                await session.rollback()
                conflicts += 1
                if not is_conflict(exc) or conflicts > MAX_CONFLICT_RETRIES:
                    raise
                continue  # another transaction touched these rows; read the keys again
        assert isinstance(result, CursorResult)  # a DELETE: the row count is known
        removed += result.rowcount
        if single:
            after = last
        if len(rows) < chunk:
            return removed
        await asyncio.sleep(0)  # let ticks and requests run between chunks


class RetentionJob:
    def __init__(self, settings: Settings, busy: Callable[[], bool] = NEVER_BUSY) -> None:
        self.busy = busy
        self.readings_hours = settings.realtime_retention_hours
        self.log_days = settings.log_retention_days
        self._task: asyncio.Task | None = None

    async def run_once(self) -> dict[str, int]:
        now = utcnow()
        # The small tables first: the readings purge can wait for the writer for a long time.
        removed = {
            "system_logs": await purge_before(SystemLog, SystemLog.created_at, now - timedelta(days=self.log_days)),
            "idempotency_keys": await purge_before(
                IdempotencyKey, IdempotencyKey.created_at, now - timedelta(hours=IDEMPOTENCY_HOURS)
            ),
            "realtime_alerts": await purge_before(
                RealtimeAlert, RealtimeAlert.started_at, now - timedelta(days=ALERT_RETENTION_DAYS)
            ),
            "realtime_readings": await purge_before(
                RealtimeReading, RealtimeReading.generated_at, now - timedelta(hours=self.readings_hours), busy=self.busy
            ),
        }
        if any(removed.values()):
            logger.info("Retention purge removed %s", removed)
        return removed

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - try again next cycle
                logger.error("Retention purge failed: %s", f"{type(exc).__name__}: {exc}"[:300])
            await asyncio.sleep(INTERVAL_SECONDS)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="retention")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
