"""Regression invariants for slow recovery, renewal, batching and report clocks."""

import asyncio
import io
from datetime import datetime, timedelta

import pytest
from app.core.config import get_settings
from app.core.time import utcnow
from app.db.session import SessionLocal
from app.models import RealtimeMinuteStat
from app.schemas.analytics import Interval, Source
from app.services import analytics, lease, realtime
from app.services.batch import BatchWriter
from app.services.realtime import CategoryStats, MinuteAccumulator
from openpyxl import load_workbook
from sqlalchemy import select


def contribution(minute: datetime, values: list[float]) -> list[dict]:
    stats = CategoryStats()
    for value in values:
        stats.add(value, "S0001", "CPU0", False)
    return MinuteAccumulator(minute=minute, stats={"cpu_temp": stats}).rows()


async def raw(minute: datetime, pairs: list[tuple[int, float]]) -> None:
    await realtime.write_readings([
        {"server": "S0001", "sensor": "CPU0", "category": "cpu_temp", "value": value,
         "threshold": 90., "is_anomaly": False, "generated_at": minute + timedelta(seconds=second)}
        for second, value in pairs
    ])


async def test_delayed_repair_does_not_erase_active_minute_or_its_receipt():
    minute = datetime(2026, 10, 14, 1, 0)
    rows = contribution(minute, [70., 80.])
    await raw(minute, [(10, 70.), (40, 80.)])
    await realtime.write_minute_stats(rows)
    assert await realtime.backfill_minute_stats(minute + timedelta(seconds=30)) == 0
    await realtime.write_minute_stats(rows)
    async with SessionLocal() as session:
        saved = await session.get(RealtimeMinuteStat, (minute, "cpu_temp"))
        assert saved is not None and (saved.count, saved.total) == (2, 150.)
    assert await realtime.backfill_minute_stats(minute + timedelta(minutes=1)) == 1


@pytest.mark.parametrize("owner,age,expected", [("other", 0, 0), ("ours", 60, 0), ("ours", 0, 1)])
async def test_repair_requires_a_current_lease_holder(owner: str, age: int, expected: int):
    now = utcnow()
    minute = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    await raw(minute, [(10, 70.)])
    await realtime.write_minute_stats(contribution(minute, [70., 80.]))
    assert await lease.claim(owner, now - timedelta(seconds=age))
    assert await realtime.backfill_minute_stats(now, holder="ours") == expected
    async with SessionLocal() as session:
        saved = await session.get(RealtimeMinuteStat, (minute, "cpu_temp"))
        assert saved is not None and saved.count == (1 if expected else 2)


async def test_repair_rechecks_ownership_after_its_raw_scan(monkeypatch):
    now = utcnow()
    minute = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    await raw(minute, [(10, 70.)])
    await realtime.write_minute_stats(contribution(minute, [70., 80.]))
    assert await lease.claim("ours", now)
    factory = realtime.SessionLocal

    def session_after_takeover():
        session = factory()
        execute = session.execute

        async def scan_then_takeover(*args, **kwargs):
            result = await execute(*args, **kwargs)
            # A long scan outlives the original lease; another process claims it.
            assert await lease.claim("other", now + timedelta(seconds=lease.TTL_SECONDS + 1))
            return result

        monkeypatch.setattr(session, "execute", scan_then_takeover)
        return session

    monkeypatch.setattr(realtime, "SessionLocal", session_after_takeover)
    assert await realtime.backfill_minute_stats(now, holder="ours") == 0
    async with SessionLocal() as session:
        saved = await session.get(RealtimeMinuteStat, (minute, "cpu_temp"))
        assert saved is not None and saved.count == 2


@pytest.mark.parametrize("fail", [False, True])
async def test_batch_total_budget_includes_a_blocked_snapshot(fail: bool):
    started, finish = asyncio.Event(), asyncio.Event()

    async def write(_rows: list[int]) -> None:
        started.set()
        await finish.wait()
        if fail:
            raise ConnectionError("lost reply")

    writer = BatchWriter("review", write, batch_size=3, interval=60, max_pending=3)
    writer.extend([1, 2, 3])
    task = asyncio.create_task(writer.flush())
    await started.wait()
    writer.add(4)
    writer.extend([5, 6])
    assert (writer.pending, writer.queued, writer.in_flight, writer.dropped) == (3, 0, 3, 3)
    finish.set()
    await task
    assert writer.in_flight == 0
    assert writer.pending == (3 if fail else 0)
    assert writer.written + writer.dropped + writer.pending == 6


async def test_cancelled_flush_returns_its_reservation_to_the_queue():
    started = asyncio.Event()

    async def write(_rows: list[int]) -> None:
        started.set()
        await asyncio.Event().wait()

    writer = BatchWriter("review", write, batch_size=2, interval=60, max_pending=3)
    writer.extend([1, 2])
    task = asyncio.create_task(writer.flush())
    await started.wait()
    writer.extend([3, 4, 5])
    assert (writer.pending, writer.queued, writer.in_flight, writer.dropped) == (3, 1, 2, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (writer.pending, writer.queued, writer.in_flight, writer.dropped) == (3, 3, 0, 2)
    assert list(writer._buffer) == [1, 2, 5]


async def test_successful_chunk_keeps_memory_reserved_until_the_whole_flush_finishes():
    slow, finish = asyncio.Event(), asyncio.Event()

    async def write(rows: list[int]) -> None:
        if rows == [2]:
            slow.set()
            await finish.wait()

    writer = BatchWriter("review", write, batch_size=2, interval=60, max_pending=2, chunk_size=1, concurrency=2)
    writer.extend([1, 2])
    task = asyncio.create_task(writer.flush())
    await slow.wait()
    writer.add(3)
    assert (writer.pending, writer.in_flight, writer.dropped) == (2, 2, 1)
    finish.set()
    assert await task == 2


async def test_lease_watchdog_stops_ticks_while_renewal_cleanup_is_blocked(monkeypatch):
    renewal, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def claim(_holder: str, _now: datetime) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        renewal.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()  # deliberately longer than the operation timeout
        return False

    monkeypatch.setattr(lease, "claim", claim)
    monkeypatch.setattr(lease, "RENEW_SECONDS", .02)
    monkeypatch.setattr(lease, "TTL_SECONDS", .12)
    hub = realtime.RealtimeHub(get_settings().model_copy(update={"realtime_interval_seconds": .01, "realtime_servers": 1}))
    hub.start()
    try:
        await asyncio.wait_for(renewal.wait(), 1)
        await asyncio.wait_for(cleanup.wait(), 1)
        await asyncio.sleep(.15)
        assert not hub.generating
        ticks = len(hub.recent)
        await asyncio.sleep(.05)
        assert len(hub.recent) == ticks
    finally:
        release.set()
        await hub.stop()


async def test_slow_successful_lease_reply_does_not_receive_a_fresh_ttl(monkeypatch):
    replied = asyncio.Event()
    calls = 0

    async def claim(_holder: str, _now: datetime) -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.Event().wait()
        await asyncio.sleep(.12)
        replied.set()
        return True

    monkeypatch.setattr(lease, "claim", claim)
    monkeypatch.setattr(lease, "RENEW_SECONDS", .15)
    monkeypatch.setattr(lease, "TTL_SECONDS", .2)
    hub = realtime.RealtimeHub(get_settings().model_copy(update={"realtime_interval_seconds": .01, "realtime_servers": 1}))
    hub.start()
    try:
        await asyncio.wait_for(replied.wait(), 1)
        await asyncio.sleep(.01)
        assert hub.generating
        assert hub._lease_deadline is not None
        assert hub._lease_deadline - asyncio.get_running_loop().time() < .09
        await asyncio.sleep(.1)
        assert not hub.generating  # expires at request-start + .2, not response + .2
    finally:
        await hub.stop()


async def test_non_holder_does_not_schedule_history_recovery(monkeypatch):
    recovered = []

    async def claim(_holder: str, _now: datetime) -> bool:
        return False

    async def rebuild(_started: datetime, *, holder: str | None = None) -> int:
        recovered.append(holder)
        return 0

    monkeypatch.setattr(lease, "claim", claim)
    monkeypatch.setattr(lease, "RENEW_SECONDS", .01)
    monkeypatch.setattr(realtime, "backfill_minute_stats", rebuild)
    hub = realtime.RealtimeHub(get_settings())
    hub.start()
    await asyncio.sleep(.05)
    await hub.stop()
    assert not hub.generating and recovered == []


async def test_first_tick_after_a_stall_rejects_an_expired_lease():
    hub = realtime.RealtimeHub(get_settings().model_copy(update={"realtime_interval_seconds": .01}))
    hub._lease_deadline = asyncio.get_running_loop().time() - 1
    task = asyncio.create_task(hub._run())  # watchdog has not had a turn yet
    try:
        await asyncio.sleep(.03)
        assert not hub.recent and hub.writer.pending == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_live_workbook_freezes_all_sheet_cutoffs_across_a_minute(monkeypatch):
    minute = datetime(2026, 10, 14, 2, 0)
    await raw(minute, [(10, 70.), (70, 80.)])
    for offset, value in enumerate([70., 80.]):
        await realtime.write_minute_stats(contribution(minute + timedelta(minutes=offset), [value]))
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return minute + timedelta(minutes=1, seconds=59) if calls <= 2 else minute + timedelta(minutes=2)

    monkeypatch.setattr(analytics, "utcnow", clock)
    async with SessionLocal() as session:
        content, _ = await analytics.export_workbook(
            session, analytics.AnalyticsFilters(source=Source.realtime, category="cpu_temp"), Interval.minute,
        )
    book = load_workbook(io.BytesIO(content))
    summary = {row[0]: row[1] for row in book["Summary"].values if row[0] is not None}
    assert summary["End"] == "2026-10-14 02:00:59 (UTC)"
    assert summary["count"] == 1
    assert list(book["By category"].values)[1][1] == 1
    assert list(book["Trend"].values)[1][2] == 1
    assert len(list(book["Data"].values)) - 1 == 1
    # The next-minute bucket really exists; its exclusion isn't an empty fixture.
    async with SessionLocal() as session:
        assert len((await session.scalars(select(RealtimeMinuteStat))).all()) == 2
