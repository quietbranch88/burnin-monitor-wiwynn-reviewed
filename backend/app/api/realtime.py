import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.api.deps import CurrentUser, EditorUser, HubDep, SessionDep, load_active_user
from app.core.config import get_settings
from app.core.errors import AuthenticationError, ValidationFailedError
from app.core.time import UTCDateTime
from app.db.session import DATABASE_ERRORS, SessionLocal, is_unavailable
from app.models import User
from app.schemas.realtime import AlertEventAcknowledge, AlertEventOut, AlertEventPage, ReadingOut, RealtimeConfig, TickOut
from app.services import alerts as alert_events

router = APIRouter(tags=["realtime"])
logger = logging.getLogger("app.realtime")

# Long-lived streams re-validate the token and account so expiry and deactivation apply.
AUTH_RECHECK_SECONDS = 30.0
# If the database is down at a re-check, keep streaming and try again this much later.
AUTH_RETRY_SECONDS = 5.0
# A re-check that has not answered by then counts as "database unavailable".
AUTH_CHECK_TIMEOUT_SECONDS = 10.0
# RFC 6455 "Try Again Later": the client should reconnect, not treat it as a logout.
WS_TRY_AGAIN_LATER = 1013
# A client that has not taken a message in this long is stuck (its TCP window is full): the
# stream closes with 1013 so it reconnects and refills, instead of holding the re-check back.
SEND_TIMEOUT_SECONDS = 10.0


@router.get("/realtime/latest", response_model=list[ReadingOut])
async def latest(
    hub: HubDep,
    _: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    category: Annotated[str | None, Query(max_length=50)] = None,
    server: Annotated[str | None, Query(max_length=20)] = None,
    sensor: Annotated[str | None, Query(max_length=20)] = None,
) -> list[ReadingOut]:
    """Readings from the most recent tick, held in memory before the batch write."""
    return [ReadingOut.model_validate(r) for r in hub.latest(limit, category, server, sensor)]


@router.get("/realtime/summary", response_model=list[TickOut])
async def summary(hub: HubDep, _: CurrentUser, seconds: Annotated[int, Query(ge=1, le=600)] = 120) -> list[TickOut]:
    """Per-tick fleet summaries for the last `seconds`, oldest first; lets a dashboard fill its charts."""
    return [TickOut.model_validate(t) for t in hub.summaries(seconds)]


@router.get("/realtime/alerts", response_model=list[ReadingOut])
async def alerts(hub: HubDep, _: CurrentUser, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[ReadingOut]:
    """Recent alerts across all servers: readings past their threshold (above it, or below it for fans)."""
    return [ReadingOut.model_validate(r) for r in hub.alerts(limit)]


@router.get("/realtime/alert-events", response_model=AlertEventPage)
async def list_alert_events(
    session: SessionDep,
    _: CurrentUser,
    server: Annotated[str | None, Query(max_length=20)] = None,
    category: Annotated[str | None, Query(max_length=50)] = None,
    sensor: Annotated[str | None, Query(max_length=20)] = None,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
    acknowledged: bool | None = None,
    before_at: Annotated[UTCDateTime | None, Query(description="With before_id: the previous page's next_before_at")] = None,
    before_id: Annotated[int | None, Query(ge=1, description="With before_at: the previous page's next_before_id")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AlertEventPage:
    """Stored alerts, newest first: one event per server, metric and sensor, from the first reading past
    the limit to the first one back inside, with the peak. Kept 30 days, after the raw readings are gone,
    so a run can be asked afterwards whether a server overheated. `start` and `end` apply to the start."""
    if (before_at is None) != (before_id is None):
        raise ValidationFailedError("before_at and before_id go together",
                                    errors=[{"field": "query.before_id", "message": "Pass both values from the previous page"}])
    items, cursor = await alert_events.list_alert_events(
        session, server=server, category=category, sensor=sensor, start=start, end=end,
        before_at=before_at, before_id=before_id, limit=limit, acknowledged=acknowledged,
    )
    return AlertEventPage(items=items,
                          next_before_at=cursor[0] if cursor else None, next_before_id=cursor[1] if cursor else None)


@router.patch("/realtime/alert-events/{event_id}", response_model=AlertEventOut)
async def acknowledge_alert_event(
    event_id: int, _data: AlertEventAcknowledge, session: SessionDep, actor: EditorUser,
) -> AlertEventOut:
    """Acknowledge an event once; repeated requests preserve the first actor and time."""
    return await alert_events.acknowledge_alert_event(session, actor, event_id)


@router.get("/realtime/config", response_model=RealtimeConfig)
async def config(hub: HubDep, _: CurrentUser) -> RealtimeConfig:
    """The fleet: servers, sensors per category, current thresholds, warning level, write settings."""
    settings = get_settings()
    return RealtimeConfig(
        interval_seconds=hub.interval,
        thresholds=hub.thresholds,
        sensors={category: list(sensors) for category, sensors in hub.sensors.items()},
        alert_below=sorted(hub.below),
        warning_percent=hub.warning_percent,
        servers=len(hub.servers),
        readings_per_second=hub.readings_per_second,
        flush_interval_seconds=hub.writer.interval,
        flush_batch_size=hub.writer.batch_size,
        retention_hours=settings.realtime_retention_hours,
    )


@router.websocket("/ws/realtime")
async def realtime_stream(websocket: WebSocket, token: str = "") -> None:
    """Push one `{"type": "tick", ...}` message per generator tick (see `TickOut`).

    The JWT goes in `Authorization: Bearer <JWT>`. A browser cannot set headers on a
    WebSocket, so `?token=` is accepted too; a URL can end up in proxy logs.
    """
    scheme, _, bearer = websocket.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and bearer:
        token = bearer
    async def check_account() -> User:
        async with SessionLocal() as session:
            return await asyncio.wait_for(load_active_user(session, token), AUTH_CHECK_TIMEOUT_SECONDS)

    try:
        user = await check_account()
    except AuthenticationError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
        return
    except (*DATABASE_ERRORS, TimeoutError) as exc:
        if not (isinstance(exc, TimeoutError) or is_unavailable(exc)):
            raise
        # The account cannot be checked. Accept, then close with "try again later": a refusal
        # before accept would reach the client as HTTP 403, which reads as a bad token.
        logger.warning("WebSocket refused while the database is unavailable: %s", type(exc).__name__)
        await websocket.accept()
        await websocket.close(code=WS_TRY_AGAIN_LATER, reason="Database unavailable, retry shortly")
        return

    hub = websocket.app.state.hub
    await websocket.accept()
    queue = hub.broadcaster.subscribe()
    logger.info("WebSocket subscribed: %s", user.username, extra={"user_id": user.id})

    async def wait_for_disconnect() -> None:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    reader = asyncio.create_task(wait_for_disconnect())
    try:
        await _stream(websocket, queue, reader, check_account)
    except WebSocketDisconnect:
        pass  # the client went away while we were closing
    finally:
        reader.cancel()
        if reader.done() and not reader.cancelled():
            reader.exception()  # retrieve it, so asyncio does not warn that it was never read
        hub.broadcaster.unsubscribe(queue)
        logger.info("WebSocket closed: %s", user.username, extra={"user_id": user.id})


async def _stream(
    websocket: WebSocket, queue: asyncio.Queue[str], reader: asyncio.Task, check_account: Callable[[], Coroutine[Any, Any, User]]
) -> None:
    """Send each message as it arrives until the client leaves or the account stops being valid."""
    loop = asyncio.get_running_loop()
    next_check = loop.time() + AUTH_RECHECK_SECONDS
    # The re-check runs beside the stream, so a slow or hung database never delays messages.
    check: asyncio.Task | None = None
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            waiting = {reader, getter} | ({check} if check else set())
            timeout = None if check else max(0.0, next_check - loop.time())
            done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                getter.cancel()
                return
            # cancel() fails only if the message arrived in the meantime; send it then.
            if getter in done or not getter.cancel():
                try:
                    await asyncio.wait_for(websocket.send_text(getter.result()), timeout=SEND_TIMEOUT_SECONDS)
                except (WebSocketDisconnect, RuntimeError):
                    return  # the client went away while we were sending
                except TimeoutError:
                    logger.warning("WebSocket client did not take a message within %.0f s; closing", SEND_TIMEOUT_SECONDS)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(websocket.close(code=WS_TRY_AGAIN_LATER, reason="Client too slow"), timeout=1)
                    return
            if check is None:
                if loop.time() >= next_check:
                    check = asyncio.create_task(check_account())
                continue
            if not check.done():
                continue
            delay = await _after_check(check, websocket)
            if delay is None:
                return
            next_check = loop.time() + delay
            check = None
    finally:
        if check is not None:
            check.cancel()
            # The driver may turn the cancel into its own error; read it so asyncio does not log
            # "Task exception was never retrieved".
            check.add_done_callback(lambda task: task.cancelled() or task.exception())


async def _after_check(check: asyncio.Task, websocket: WebSocket) -> float | None:
    """The re-check finished: seconds until the next one, or None when the stream must end."""
    try:
        check.result()
    except AuthenticationError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token expired or account disabled")
        return None
    except (*DATABASE_ERRORS, TimeoutError) as exc:
        if not (isinstance(exc, TimeoutError) or is_unavailable(exc)):
            raise
        # Live data does not need the database. The account was valid at the last check, so
        # keep streaming and check again soon.
        logger.warning("WebSocket re-check deferred, database unavailable: %s", type(exc).__name__)
        return AUTH_RETRY_SECONDS
    return AUTH_RECHECK_SECONDS
