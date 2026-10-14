"""Minute rollups count generated contributions once across ambiguous commits."""

from datetime import datetime, timedelta

import pytest
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import RealtimeMinuteStat
from app.services import realtime
from app.services.batch import BatchWriter
from app.services.realtime import (
    CategoryStats,
    MinuteAccumulator,
    backfill_minute_stats,
    write_minute_stats,
    write_readings,
)
from sqlalchemy.exc import DataError

BASE = datetime(2026, 10, 1, 8, 0)


def contribution(values, minute=BASE):
    stats = CategoryStats()
    for value in values:
        stats.add(value, "S0001", "CPU0", value > 90)
    return MinuteAccumulator(minute=minute, stats={"cpu_temp": stats}).rows()


async def saved(minute=BASE):
    async with SessionLocal() as session:
        stat = await session.get(RealtimeMinuteStat, (minute, "cpu_temp"))
        return stat.count, stat.total, stat.minimum, stat.maximum, stat.anomalies


async def test_committed_minute_with_lost_ack_is_counted_once():
    first = True

    async def lose_first_ack(rows):
        nonlocal first
        await write_minute_stats(rows)  # Real MariaDB commit completes before injected transport failure.
        if first:
            first = False
            raise ConnectionError("commit acknowledgement lost")

    writer = BatchWriter("minute-test", lose_first_ack, batch_size=1, interval=60, max_pending=10)
    writer.extend(contribution([70, 95]))
    assert await writer.flush() == 0
    assert writer.pending == 1
    assert await writer.flush() == 1
    assert writer.pending == 0
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)


async def test_distinct_identical_partial_contributions_and_retries_merge_once():
    before_restart = contribution([70, 95])
    after_restart = contribution([70, 95])
    await write_minute_stats(before_restart)
    await write_minute_stats(after_restart)
    await write_minute_stats(before_restart)
    await write_minute_stats(after_restart)
    assert await saved() == (4, 330.0, 70.0, 95.0, 2)


async def test_retries_remain_idempotent_after_batch_chunk_boundaries_change():
    rows = contribution([70, 95]) + contribution([60]) + contribution([100], BASE + timedelta(minutes=1))
    await write_minute_stats(rows)
    for row in reversed(rows):
        await write_minute_stats([dict(row)])
    assert await saved() == (3, 225.0, 60.0, 95.0, 1)
    assert await saved(BASE + timedelta(minutes=1)) == (1, 100.0, 100.0, 100.0, 1)


async def test_failed_transaction_does_not_leave_a_contribution_receipt():
    await write_minute_stats(contribution([70]))
    pending = contribution([95])
    bad = contribution([100], BASE + timedelta(minutes=1))[0] | {"category": "x" * 51}
    with pytest.raises(DataError):
        await write_minute_stats(pending + [bad])
    assert await saved() == (1, 70.0, 70.0, 70.0, 0)
    await write_minute_stats(pending)
    await write_minute_stats(pending)
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)


async def test_retried_minute_does_not_bias_unequal_weight_fleet_analytics(client, accounts):
    first = contribution([70, 80])
    await write_minute_stats(first)
    await write_minute_stats(first)
    await write_minute_stats(contribution([100], BASE + timedelta(minutes=1)))
    response = await client.get(
        "/api/v1/analytics/summary", params={"source": "realtime", "category": "cpu_temp"},
        headers=accounts.viewer.headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "count": 3, "total": 250.0, "average": pytest.approx(250 / 3), "minimum": 70.0, "maximum": 100.0,
    }


async def test_startup_repairs_an_older_bucket_and_keeps_replay_receipts():
    original = contribution([70, 95])
    await write_minute_stats(original)
    await write_minute_stats(contribution([100], BASE + timedelta(minutes=1)))
    raw = [
        {"server": "S0001", "sensor": "CPU0", "category": "cpu_temp", "value": value,
         "threshold": 90, "is_anomaly": value > 90, "generated_at": BASE + timedelta(seconds=second)}
        for second, value in ((10, 70), (20, 95), (65, 100))
    ]
    await write_readings(raw)
    async with SessionLocal() as session:
        old = await session.get(RealtimeMinuteStat, (BASE, "cpu_temp"))
        old.count, old.total, old.anomalies = 4, 330.0, 2  # Simulate a duplicate contribution.
        await session.commit()
    assert await backfill_minute_stats(BASE + timedelta(minutes=2)) == 2
    await write_minute_stats(original)
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)
    await write_minute_stats(contribution([60]))
    assert await saved() == (3, 225.0, 60.0, 95.0, 1)


async def test_backfill_keeps_older_aggregates_outside_recovery_window():
    original = contribution([70, 95], BASE - timedelta(hours=3))
    await write_minute_stats(original)
    assert await backfill_minute_stats(BASE) == 0
    assert await saved(BASE - timedelta(hours=3)) == (2, 165.0, 70.0, 95.0, 1)


async def test_late_raw_commit_is_recovered_on_next_start_even_with_a_later_bucket():
    original = contribution([70, 95])
    await write_minute_stats(original)
    raw = [
        {"server": "S0001", "sensor": "CPU0", "category": "cpu_temp", "value": value,
         "threshold": 90, "is_anomaly": value > 90, "generated_at": BASE + timedelta(seconds=second)}
        for second, value in ((10, 70), (20, 95))
    ]
    await write_readings(raw[:1])
    await backfill_minute_stats(BASE + timedelta(seconds=30))
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)  # The startup partial minute is never replaced.
    await write_readings(raw[1:])
    await write_minute_stats(contribution([100], BASE + timedelta(minutes=1)))
    await backfill_minute_stats(BASE + timedelta(minutes=2))
    await write_minute_stats(original)
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)


async def test_short_retention_does_not_rebuild_a_partly_expired_boundary_minute(monkeypatch):
    settings = get_settings().model_copy(update={"realtime_retention_hours": 0.5})
    monkeypatch.setattr(realtime, "get_settings", lambda: settings, raising=False)
    await write_minute_stats(contribution([70, 95]))
    await write_readings([
        {"server": "S0001", "sensor": "CPU0", "category": "cpu_temp", "value": 95,
         "threshold": 90, "is_anomaly": True, "generated_at": BASE + timedelta(seconds=40)},
    ])
    # At 08:30:30 the first half of 08:00 can already be gone. Preserve that whole bucket.
    assert await backfill_minute_stats(BASE + timedelta(minutes=30, seconds=30)) == 0
    assert await saved() == (2, 165.0, 70.0, 95.0, 1)
