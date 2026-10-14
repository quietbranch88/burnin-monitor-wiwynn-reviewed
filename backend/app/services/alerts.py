"""Alert events: stored per server and sensor, queried after the raw readings are gone."""

from datetime import datetime

from sqlalchemy import CursorResult, and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.time import to_storage, utcnow
from app.db.session import DUPLICATE_KEY, SessionLocal, error_code
from app.models.alert import RealtimeAlert
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.realtime import AlertEventOut
from app.services.system_logs import current_request_id

# Longer than a burn-in run (1 to 2 days) by a wide margin, and far smaller than the raw
# readings: a few events a minute at the demo's alert rate.
RETENTION_DAYS = 30


def _key(event: dict) -> tuple[str, str, str, datetime]:
    return event["server"], event["category"], event["sensor"], event["started_at"]


async def write_alerts(batch: list[dict]) -> None:
    """ORM bulk INSERT of closed (or, at a stop, still open) alert events, safe to retry.

    As with the readings: a retry after a lost reply hits the unique (server, category,
    sensor, started_at) key, and only the events not stored yet are written then.
    """
    async with SessionLocal() as session:
        try:
            await session.execute(insert(RealtimeAlert), batch)
            await session.commit()
            return
        except IntegrityError as exc:
            if error_code(exc) != DUPLICATE_KEY:
                raise
            await session.rollback()
        result = await session.execute(
            select(RealtimeAlert.server, RealtimeAlert.category, RealtimeAlert.sensor, RealtimeAlert.started_at).where(
                RealtimeAlert.server.in_({e["server"] for e in batch}),
                RealtimeAlert.started_at.in_({e["started_at"] for e in batch}),
            )
        )
        stored = {tuple(row) for row in result}
        missing = [e for e in batch if _key(e) not in stored]
        if missing:
            await session.execute(insert(RealtimeAlert), missing)
        await session.commit()


async def list_alert_events(
    session: AsyncSession,
    *,
    server: str | None,
    category: str | None,
    sensor: str | None,
    start: datetime | None,
    end: datetime | None,
    before_at: datetime | None,
    before_id: int | None,
    limit: int,
    acknowledged: bool | None = None,
) -> tuple[list[AlertEventOut], tuple[datetime, int] | None]:
    """Newest start first, ties by id; an exclusive (started_at, id) cursor gives the next page."""
    conditions = []
    if server:
        conditions.append(RealtimeAlert.server == server)
    if category:
        conditions.append(RealtimeAlert.category == category.strip().lower())
    if sensor:
        conditions.append(RealtimeAlert.sensor == sensor)
    if start is not None:
        conditions.append(RealtimeAlert.started_at >= to_storage(start))
    if end is not None:
        conditions.append(RealtimeAlert.started_at <= to_storage(end))
    if acknowledged is not None:
        conditions.append(RealtimeAlert.acknowledged_at.is_not(None) if acknowledged else RealtimeAlert.acknowledged_at.is_(None))
    if before_at is not None and before_id is not None:
        at = to_storage(before_at)
        conditions.append(or_(RealtimeAlert.started_at < at, and_(RealtimeAlert.started_at == at, RealtimeAlert.id < before_id)))
    query = (
        select(RealtimeAlert, User.username).outerjoin(User, RealtimeAlert.acknowledged_by == User.id)
        .where(*conditions).order_by(RealtimeAlert.started_at.desc(), RealtimeAlert.id.desc()).limit(limit + 1)
    )
    rows = [_event_out(event, username) for event, username in await session.execute(query)]
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].started_at, rows[-1].id) if more else None


def _event_out(event: RealtimeAlert, username: str | None) -> AlertEventOut:
    return AlertEventOut(
        id=event.id, server=event.server, category=event.category, sensor=event.sensor,
        started_at=event.started_at, ended_at=event.ended_at, threshold=event.threshold,
        peak_value=event.peak_value, readings=event.readings,
        acknowledged_at=event.acknowledged_at, acknowledged_by=username,
    )


async def acknowledge_alert_event(session: AsyncSession, actor: User, event_id: int) -> AlertEventOut:
    """The first conditional update wins; the acknowledgement and its audit log commit together."""
    # Authentication read the user first. End that snapshot so MariaDB can wait for a
    # competing update and recheck the predicate instead of refusing a stale snapshot.
    await session.commit()
    at = utcnow()
    result = await session.execute(
        update(RealtimeAlert).where(RealtimeAlert.id == event_id, RealtimeAlert.acknowledged_at.is_(None))
        .values(acknowledged_at=at, acknowledged_by=actor.id),
        execution_options={"synchronize_session": False},
    )
    assert isinstance(result, CursorResult)
    if result.rowcount:
        session.add(SystemLog(
            created_at=at, level="INFO", logger="app.alerts",
            message=f"Alert event {event_id} acknowledged by {actor.username}",
            user_id=actor.id, request_id=current_request_id.get(),
        ))
    # End the authentication read snapshot before reading another request's winning update.
    await session.commit()
    row = (await session.execute(
        select(RealtimeAlert, User.username).outerjoin(User, RealtimeAlert.acknowledged_by == User.id)
        .where(RealtimeAlert.id == event_id).execution_options(populate_existing=True)
    )).one_or_none()
    if row is None:
        raise NotFoundError("Alert event not found")
    return _event_out(row[0], row[1])
