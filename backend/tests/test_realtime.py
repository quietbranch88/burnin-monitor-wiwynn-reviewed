import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta

import pytest
import websockets
from app.api import realtime as realtime_api
from app.core.config import get_settings
from app.core.time import utcnow
from app.db.session import SessionLocal
from app.models import IdempotencyKey, RealtimeAlert, RealtimeMinuteStat, RealtimeReading, SystemLog
from app.services import realtime as realtime_service
from app.services import retention as retention_service
from app.services.batch import BatchWriter
from app.services.realtime import ALERT_MIN_READINGS, RealtimeHub, write_minute_stats, write_readings
from app.services.retention import RetentionJob, purge_before
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


def fleet(servers: int):
    return get_settings().model_copy(update={"realtime_servers": servers})


def test_tick_covers_every_server_and_sensor():
    hub = RealtimeHub(fleet(3), seed=11)
    message = hub.tick()
    readings = hub.last_readings
    assert hub.sensors_per_server == 50 and len(hub.sensors["gpu_temp"]) == 8 and len(hub.sensors["dimm_temp"]) == 16
    assert len(readings) == 3 * 50 == message["readings"]
    assert {(r["server"], r["category"], r["sensor"]) for r in readings} == {
        (s, c, sensor) for s in ("S0001", "S0002", "S0003") for c, sensors in hub.sensors.items() for sensor in sensors
    }
    below = {"fan_rpm"}
    assert all(r["is_anomaly"] == ((r["value"] < r["threshold"]) if r["category"] in below else (r["value"] > r["threshold"]))
               for r in readings)
    for category, summary in message["summary"].items():
        values = [r["value"] for r in readings if r["category"] == category]
        assert summary["count"] == 3 * len(hub.sensors[category]) == len(values)
        assert summary["maximum"] == max(values) and summary["minimum"] == min(values)
        pick = min if summary["alert_below"] else max
        worst = next(r for r in readings if r["category"] == category and r["value"] == pick(values))
        assert summary["worst"] == worst["value"]
        assert (summary["worst_server"], summary["worst_sensor"]) == (worst["server"], worst["sensor"])
        # Within the rounding: Python 3.12's sum() adds floats more exactly than the running total.
        assert summary["average"] == pytest.approx(sum(values) / len(values), abs=0.01)
        limit = summary["threshold"]
        over = (lambda v, limit=limit: v < limit) if category in below else (lambda v, limit=limit: v > limit)
        assert summary["anomalies"] == sum(map(over, values))
    from app.services.realtime import load

    loads = [load(h["value"], h["threshold"], h["category"] in below) for h in message["hottest"]]
    assert loads == sorted(loads, reverse=True)
    assert all(a["is_anomaly"] for a in message["alerts"])
    json.dumps(message)  # ready for the wire


def test_tick_pushes_each_servers_hottest_sensor_only_for_a_line_sized_fleet():
    small = RealtimeHub(fleet(200), seed=3)
    pushed = small.tick()
    assert pushed["readings"] == 200 * 50
    per_server = pushed["server_readings"]
    assert len(per_server) == 200 * len(small.thresholds)
    assert set(per_server[0]) == {"server", "category", "sensor", "value"}  # time and threshold are in the tick
    for r in per_server[:20]:
        same = [x["value"] for x in small.last_readings if (x["server"], x["category"]) == (r["server"], r["category"])]
        nearest = min if r["category"] in small.below else max  # the slowest fan, the hottest of the rest
        assert len(same) == len(small.sensors[r["category"]]) and r["value"] == nearest(same)
    assert "server_readings" not in small.recent[-1]  # late joiners get the summary only
    assert "server_readings" not in RealtimeHub(fleet(201), seed=3).tick()


def test_a_category_without_a_sensor_list_reports_one_sensor():
    hub = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 2, "realtime_thresholds": {"humidity": 80.0}}), seed=1)
    hub.tick()
    assert hub.sensors == {"humidity": ("Main",)} and hub.readings_per_second == 2
    assert [(r["server"], r["sensor"]) for r in hub.last_readings] == [("S0001", "Main"), ("S0002", "Main")]


def test_tick_message_stays_small_for_a_large_fleet():
    hub = RealtimeHub(fleet(400), seed=5)
    message = hub.tick()
    assert message["readings"] == 20_000 and hub.writer.pending == 20_000
    # Bounded by the alert and hottest caps, not by the number of servers.
    assert len(json.dumps(message)) < 20_000


class Recorder:
    def __init__(self, failures: int = 0) -> None:
        self.batches: list[list[int]] = []
        self.failures = failures

    async def __call__(self, batch: list[int]) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database down")
        self.batches.append(batch)


async def test_batch_writer_flushes_when_buffer_is_full():
    recorder = Recorder()
    writer = BatchWriter("t", recorder, batch_size=3, interval=60, max_pending=100)
    writer.start()
    for i in range(3):
        writer.add(i)
    await asyncio.sleep(0.05)
    assert recorder.batches == [[0, 1, 2]] and writer.pending == 0
    await writer.stop()


async def test_batch_writer_flushes_on_interval_and_on_stop():
    recorder = Recorder()
    writer = BatchWriter("t", recorder, batch_size=100, interval=0.05, max_pending=100)
    writer.start()
    writer.add(1)
    await asyncio.sleep(0.15)
    assert recorder.batches == [[1]]
    writer.add(2)
    await writer.stop()
    assert recorder.batches == [[1], [2]]


async def test_batch_writer_keeps_items_after_a_failed_write():
    recorder = Recorder(failures=1)
    writer = BatchWriter("t", recorder, batch_size=100, interval=60, max_pending=100)
    writer.add(1)
    writer.add(2)
    assert await writer.flush() == 0
    assert writer.pending == 2 and writer.last_error == "RuntimeError: database down"
    writer.add(3)
    assert await writer.flush() == 3
    assert recorder.batches == [[1, 2, 3]] and writer.last_error is None and writer.written == 3


class RejectedRow(Exception):
    pass


async def test_batch_writer_drops_only_rejected_rows():
    written: list[list[int]] = []

    async def write(batch: list[int]) -> None:
        if -1 in batch:
            raise RejectedRow("data too long")
        written.append(batch)

    writer = BatchWriter("t", write, batch_size=100, interval=60, max_pending=100,
                         is_permanent=lambda e: isinstance(e, RejectedRow))
    for item in (1, -1, 2):
        writer.add(item)
    assert await writer.flush() == 2
    assert written == [[1], [2]] and writer.dropped == 1 and writer.pending == 0
    writer.add(3)
    assert await writer.flush() == 1 and written[-1] == [3]


async def test_row_by_row_retry_requeues_only_unwritten_items():
    """System logs have no unique key, so a row stored before an outage must not be retried."""
    written: list[list[int]] = []
    down = False

    async def write(batch: list[int]) -> None:
        nonlocal down
        if -1 in batch:
            raise RejectedRow("data too long")
        if down:
            raise RuntimeError("database down")
        written.append(batch)
        down = batch == [1]  # the database goes away right after the first row

    writer = BatchWriter("t", write, batch_size=100, interval=60, max_pending=100,
                         is_permanent=lambda e: isinstance(e, RejectedRow))
    for item in (1, 2, -1, 3):
        writer.add(item)
    assert await writer.flush() == 1 and writer.pending == 3
    down = False
    assert await writer.flush() == 2
    assert written == [[1], [2], [3]] and writer.dropped == 1 and writer.written == 3


async def test_cancelled_row_by_row_retry_does_not_requeue_stored_rows():
    written: list[list[int]] = []
    hang = asyncio.Event()

    async def write(batch: list[int]) -> None:
        if -1 in batch:
            raise RejectedRow("data too long")
        if batch == [2]:
            await hang.wait()  # shutdown arrives while this row is being written
        written.append(batch)

    writer = BatchWriter("t", write, batch_size=100, interval=60, max_pending=100,
                         is_permanent=lambda e: isinstance(e, RejectedRow))
    for item in (1, 2, -1, 3):
        writer.add(item)
    flush = asyncio.create_task(writer.flush())
    await asyncio.sleep(0.05)
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert written == [[1]] and writer.written == 1
    assert list(writer._buffer) == [2, -1, 3]  # row 1 is stored and not put back


async def test_batch_writer_stop_waits_for_in_flight_write():
    written: list[list[int]] = []
    started = asyncio.Event()

    async def slow_write(batch: list[int]) -> None:
        started.set()
        await asyncio.sleep(0.2)
        written.append(batch)

    writer = BatchWriter("t", slow_write, batch_size=2, interval=60, max_pending=100)
    writer.start()
    writer.add(1)
    writer.add(2)
    await started.wait()
    writer.add(3)
    await writer.stop()
    assert written == [[1, 2], [3]] and writer.written == 3


async def test_batch_writer_counts_every_item_lost_on_requeue():
    writer = BatchWriter("t", Recorder(failures=1), batch_size=100, interval=60, max_pending=3)
    for i in range(3):
        writer.add(i)

    async def fill_during_write(_batch):
        for i in (10, 11):
            writer.add(i)  # new items arrive while the failing write is in flight
        raise RuntimeError("database down")

    writer._write = fill_during_write
    await writer.flush()
    assert writer.pending == 3 and writer.dropped == 2


async def test_batch_writer_drops_oldest_when_full():
    writer = BatchWriter("t", Recorder(), batch_size=100, interval=60, max_pending=2)
    for i in range(3):
        writer.add(i)
    assert writer.pending == 2 and writer.dropped == 1


async def test_batch_writer_writes_chunks_in_parallel_and_requeues_only_failed_ones():
    in_flight = peak = 0
    written: list[list[int]] = []

    async def write(chunk: list[int]) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if 4 in chunk:
            raise RuntimeError("database down")
        written.append(chunk)

    writer = BatchWriter("t", write, batch_size=100, interval=60, max_pending=100, chunk_size=2, concurrency=2)
    for i in range(7):
        writer.add(i)
    assert await writer.flush() == 5
    assert peak == 2
    assert sorted(written) == [[0, 1], [2, 3], [6]]
    # Only the failed chunk is kept, so a retry cannot duplicate the chunks already stored.
    assert writer.pending == 2 and writer.written == 5 and writer.last_error == "RuntimeError: database down"


async def test_batch_writer_backs_off_after_a_failure_and_probes_with_one_chunk():
    calls: list[list[int]] = []
    down = True

    async def write(chunk: list[int]) -> None:
        calls.append(chunk)
        if down:
            raise RuntimeError("database down")

    writer = BatchWriter("t", write, batch_size=100, interval=60, max_pending=100, chunk_size=2, concurrency=4)
    for i in range(5):
        writer.add(i)
    assert await writer.flush() == 0 and writer.backoff_seconds == 1.0
    assert len(calls) == 3  # the first flush tries every chunk
    assert await writer.flush() == 0 and writer.backoff_seconds == 2.0
    assert calls[3:] == [[0, 1]] and writer.pending == 5  # only the probe reached the database; nothing reordered
    assert await writer.flush() == 0 and writer.backoff_seconds == 4.0
    down = False
    assert await writer.flush() == 5 and writer.backoff_seconds == 0.0
    assert calls[5:] == [[0, 1], [2, 3], [4]]  # the probe succeeded, then the rest in one go
    assert writer.pending == 0 and writer.written == 5 and writer.last_error is None


async def test_batch_writer_waits_before_retrying_a_failed_write():
    attempts: list[float] = []

    async def write(_chunk: list[int]) -> None:
        attempts.append(time.monotonic())
        raise RuntimeError("database down")

    writer = BatchWriter("t", write, batch_size=1, interval=0.01, max_pending=10)
    writer.start()
    writer.add(1)
    await asyncio.sleep(0.3)
    assert len(attempts) == 1  # the first retry waits 0.5 to 1 s; before the backoff this loop retried every 10 ms
    started = time.monotonic()
    await writer.stop()
    assert time.monotonic() - started < 0.3  # the stop ends the wait at once instead of after the backoff
    assert len(attempts) <= 3 and writer.dropped == 1


async def test_hub_tick_publishes_once_and_persists_every_reading():
    hub = RealtimeHub(fleet(5), seed=7)
    queue = hub.broadcaster.subscribe()
    hub.tick()
    hub.tick()
    assert queue.qsize() == 2 and json.loads(queue.get_nowait())["type"] == "tick"
    assert await hub.writer.flush() == 2 * 5 * 50
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RealtimeReading)) == 500
        assert await session.scalar(select(func.count(func.distinct(RealtimeReading.server)))) == 5
        assert await session.scalar(select(func.count(func.distinct(RealtimeReading.sensor)))) == 50


async def test_a_retried_batch_is_stored_once():
    hub = RealtimeHub(fleet(3), seed=8)
    hub.tick()
    batch = hub.last_readings
    await write_readings(batch)
    await write_readings(batch)  # the commit succeeded, but the writer never heard back
    async with SessionLocal() as session:
        stored = (await session.execute(select(func.count()).select_from(RealtimeReading))).scalar_one()
    assert stored == len(batch) == 150


async def test_an_alert_becomes_one_event_from_the_first_reading_over_the_limit_to_the_first_back_inside():
    hub = RealtimeHub(fleet(2), seed=6)
    cpu_limit = hub.thresholds["cpu_temp"]
    hub.thresholds["cpu_temp"] = 1.0  # every CPU reading (about 60 °C) is now past the limit
    seen: list[dict] = []
    for _ in range(ALERT_MIN_READINGS):  # still over: the events stay open and count their readings
        hub.tick()
        seen.append({(r["server"], r["sensor"]): r["value"] for r in hub.last_readings if r["category"] == "cpu_temp"})
    assert hub.alert_writer.pending == 0  # nothing is stored before an alert ends
    hub.thresholds["cpu_temp"] = cpu_limit
    hub.tick()  # back inside: the events close and are queued
    assert await hub.alert_writer.flush() >= 4
    async with SessionLocal() as session:
        events = (await session.scalars(
            select(RealtimeAlert).where(RealtimeAlert.category == "cpu_temp").order_by(RealtimeAlert.server, RealtimeAlert.sensor)
        )).all()
    n = ALERT_MIN_READINGS
    assert [(e.server, e.sensor, e.readings) for e in events] == [
        ("S0001", "CPU0", n), ("S0001", "CPU1", n), ("S0002", "CPU0", n), ("S0002", "CPU1", n),
    ]
    assert all(e.started_at < e.ended_at and e.threshold == 1.0 for e in events)
    assert [e.peak_value for e in events] == [max(tick[k] for tick in seen) for k in sorted(seen[0])]  # the peak over all ticks
    # A stop closes the alerts still open at that moment and stores them, instead of losing them:
    # this process will not see those sensors come back inside their limit.
    stopped = RealtimeHub(fleet(1), seed=6)
    stopped.thresholds["cpu_temp"] = 1.0
    for _ in range(ALERT_MIN_READINGS):
        stopped.tick()
    before = utcnow()
    await stopped.stop()
    async with SessionLocal() as session:
        closed_at_stop = (await session.scalars(select(RealtimeAlert).where(RealtimeAlert.server == "S0001",
                                                                             RealtimeAlert.ended_at >= before))).all()
    assert {(e.server, e.sensor) for e in closed_at_stop} >= {("S0001", "CPU0"), ("S0001", "CPU1")}
    assert all(e.ended_at is not None and e.started_at <= e.ended_at for e in closed_at_stop)


async def test_a_spike_lasts_several_ticks_so_an_alert_has_a_duration_and_a_peak():
    hub = RealtimeHub(fleet(20), seed=12)
    for _ in range(60):
        hub.tick()
    await hub.alert_writer.flush()
    async with SessionLocal() as session:
        events = (await session.scalars(select(RealtimeAlert))).all()
    assert events, "sixty ticks of twenty servers spike somewhere"
    assert all(e.started_at < e.ended_at and e.readings >= ALERT_MIN_READINGS for e in events)  # no one-second events
    assert max(e.readings for e in events) > ALERT_MIN_READINGS


async def test_an_alert_shorter_than_the_minimum_is_not_stored_as_an_event():
    hub = RealtimeHub(fleet(1), seed=6)
    cpu_limit = hub.thresholds["cpu_temp"]
    hub.thresholds["cpu_temp"] = 1.0
    for _ in range(ALERT_MIN_READINGS - 1):
        hub.tick()
    assert all(r["is_anomaly"] for r in hub.last_readings if r["category"] == "cpu_temp")  # still flagged on the reading
    hub.thresholds["cpu_temp"] = cpu_limit
    hub.tick()
    assert hub.alert_writer.pending == 0 and hub.short_alerts >= 2  # CPU0 and CPU1: too short for an event


async def test_a_retried_alert_batch_is_stored_once():
    from app.services.alerts import write_alerts

    at = utcnow()
    batch = [{"server": "S0001", "category": "cpu_temp", "sensor": "CPU0", "started_at": at, "ended_at": at + timedelta(seconds=3),
              "threshold": 90.0, "peak_value": 97.5, "readings": 3}]
    await write_alerts(batch)
    await write_alerts(batch)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RealtimeAlert)) == 1


async def test_alert_events_are_listed_newest_first_with_filters_and_a_cursor(client, accounts):
    base = datetime(2026, 10, 1, 0, 0, 0)
    rows = [("S0001", "cpu_temp", "CPU0", 0), ("S0002", "gpu_temp", "GPU1", 10), ("S0001", "cpu_temp", "CPU1", 20)]
    async with SessionLocal() as session:
        session.add_all(RealtimeAlert(server=s, category=c, sensor=n, started_at=base + timedelta(seconds=sec),
                                      ended_at=base + timedelta(seconds=sec + 2), threshold=90, peak_value=95, readings=2)
                        for s, c, n, sec in rows)
        await session.commit()
    url, headers = "/api/v1/realtime/alert-events", accounts.viewer.headers  # any signed-in user may ask
    first = (await client.get(url, params={"limit": 2}, headers=headers)).json()
    assert [(e["server"], e["sensor"]) for e in first["items"]] == [("S0001", "CPU1"), ("S0002", "GPU1")]
    cursor = {"before_at": first["next_before_at"], "before_id": first["next_before_id"]}
    second = (await client.get(url, params={"limit": 2, **cursor}, headers=headers)).json()
    assert [(e["server"], e["sensor"]) for e in second["items"]] == [("S0001", "CPU0")] and second["next_before_id"] is None
    one_server = (await client.get(url, params={"server": "S0001", "category": "CPU_Temp"}, headers=headers)).json()
    assert [e["sensor"] for e in one_server["items"]] == ["CPU1", "CPU0"]
    window = (await client.get(url, params={"start": "2026-10-01T00:00:05Z", "end": "2026-10-01T00:00:15Z"}, headers=headers)).json()
    assert [e["server"] for e in window["items"]] == ["S0002"]
    assert (await client.get(url, params={"before_id": 1}, headers=headers)).status_code == 422


async def test_a_partly_stored_batch_is_completed_without_duplicates():
    hub = RealtimeHub(fleet(5), seed=9)
    hub.tick()
    batch = hub.last_readings
    await write_readings(batch[:7])  # an earlier attempt stored part of it: some of a server's sensors
    await write_readings(batch)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RealtimeReading)) == len(batch) == 250


def test_a_slow_client_loses_its_oldest_messages():
    from app.services.realtime import Broadcaster

    broadcaster = Broadcaster(queue_size=2)
    queue = broadcaster.subscribe()
    for text in ("1", "2", "3"):
        broadcaster.publish(text)
    assert [queue.get_nowait(), queue.get_nowait()] == ["2", "3"]


async def test_retention_job_runs_on_start_and_stops_cleanly():
    old = utcnow() - timedelta(days=60)
    async with SessionLocal() as session:
        session.add(SystemLog(created_at=old, level="INFO", logger="app.test", message="old"))
        await session.commit()
    job = RetentionJob(get_settings())
    job.start()
    await asyncio.sleep(0.2)
    await job.stop()
    async with SessionLocal() as session:
        # Only the expired line: other tests' log lines may still arrive from the writer's buffer.
        assert await session.scalar(select(func.count()).select_from(SystemLog).where(SystemLog.message == "old")) == 0


async def test_stopping_a_writer_whose_database_hangs_counts_what_is_lost(monkeypatch):
    from app.services import batch

    monkeypatch.setattr(batch, "STOP_TIMEOUT_SECONDS", 0.1)

    async def hangs(_items):
        await asyncio.Event().wait()

    writer = BatchWriter("t", hangs, batch_size=2, interval=60, max_pending=100)
    writer.start()
    writer.extend([1, 2, 3])
    await asyncio.wait_for(writer.stop(), timeout=2)
    # Nothing was written, and every item is counted instead of vanishing.
    assert writer.written == 0 and writer.dropped == 3 and writer.pending == 0


async def test_minute_statistics_merge_partial_writes():
    minute = datetime(2026, 10, 1, 8, 0)
    first = {"minute": minute, "category": "cpu_temp", "contribution_id": "first", "count": 2, "total": 150.0,
             "minimum": 70.0, "maximum": 80.0, "anomalies": 0}
    second = first | {"contribution_id": "second", "count": 3, "total": 300.0, "minimum": 60.0, "maximum": 99.0, "anomalies": 1}
    await write_minute_stats([first])
    await write_minute_stats([second])  # same minute again, e.g. after a restart
    async with SessionLocal() as session:
        stat = await session.scalar(select(RealtimeMinuteStat))
    assert (stat.count, stat.total, stat.minimum, stat.maximum, stat.anomalies) == (5, 450.0, 60.0, 99.0, 1)


async def test_minute_statistics_keep_full_precision():
    row = {"minute": datetime(2026, 10, 1, 9, 0), "category": "fan_rpm", "contribution_id": "precision", "count": 8800, "total": 132_000_123.45,
           "minimum": 9000.25, "maximum": 15000.55, "anomalies": 3}
    await write_minute_stats([row])
    async with SessionLocal() as session:
        stat = await session.scalar(select(RealtimeMinuteStat))
    assert (stat.total, stat.minimum, stat.maximum) == (132_000_123.45, 9000.25, 15000.55)


def test_minute_rollover_queues_the_finished_minute():
    hub = RealtimeHub(fleet(2), seed=9)
    stats = hub.tick()["summary"]
    assert hub.minute_writer.pending == 0
    hub._accumulate(hub._minute.minute + timedelta(minutes=1), {})
    queued = list(hub.minute_writer._buffer)
    assert {r["category"] for r in queued} == set(stats)
    assert all(r["count"] == 2 * len(hub.sensors[r["category"]]) for r in queued)


async def test_retention_purges_only_expired_rows(accounts):
    old, recent = utcnow() - timedelta(hours=48), utcnow()
    async with SessionLocal() as session:
        session.add_all(
            RealtimeReading(server=f"S000{i}", category="cpu_temp", sensor="CPU0", value=1, threshold=90, is_anomaly=False, generated_at=t)
            for i, t in enumerate((old, old, old, recent), start=1)
        )
        session.add_all(SystemLog(created_at=t, level="INFO", logger="app.test", message="m") for t in (old - timedelta(days=30), recent))
        session.add_all(
            IdempotencyKey(user_id=accounts.engineer.user.id, key=key, request_hash="h", response={}, created_at=t)
            for key, t in (("expired", old), ("fresh", recent))
        )
        session.add_all(
            RealtimeAlert(server="S0001", category="cpu_temp", sensor="CPU0", started_at=t, ended_at=t, threshold=90,
                          peak_value=95, readings=1)
            for t in (old - timedelta(days=40), recent)
        )
        await session.commit()
    removed = await RetentionJob(get_settings()).run_once()
    assert removed == {"realtime_readings": 3, "system_logs": 1, "idempotency_keys": 1, "realtime_alerts": 1}
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RealtimeReading)) == 1


async def test_purge_works_in_chunks():
    old = utcnow() - timedelta(hours=48)
    async with SessionLocal() as session:
        session.add_all(
            RealtimeReading(server="S0001", category="cpu_temp", sensor="CPU0", value=1, threshold=90, is_anomaly=False,
                            generated_at=old + timedelta(seconds=i))
            for i in range(5)
        )
        await session.commit()
    assert await purge_before(RealtimeReading, RealtimeReading.generated_at, utcnow(), chunk=2) == 5


async def test_purge_chunks_stay_small_when_a_reading_was_stored_late(monkeypatch):
    """A reading written after an outage has a higher id but an older time than rows stored
    before it. It must not widen one chunk's key range over every expired row."""
    old = utcnow() - timedelta(hours=48)
    async with SessionLocal() as session:
        for at in [old + timedelta(seconds=i) for i in range(1, 6)] + [old]:  # the last one is stored late
            session.add(RealtimeReading(server="S0001", category="cpu_temp", sensor="CPU0", value=1, threshold=90,
                                        is_anomaly=False, generated_at=at))
            await session.flush()
        await session.commit()
    deleted = []
    real_execute = AsyncSession.execute

    async def spy(self, statement, *args, **kwargs):
        result = await real_execute(self, statement, *args, **kwargs)
        if statement.is_delete:
            deleted.append(result.rowcount)
        return result

    monkeypatch.setattr(AsyncSession, "execute", spy)
    assert await purge_before(RealtimeReading, RealtimeReading.generated_at, utcnow(), chunk=2) == 6
    assert deleted == [2, 2, 2]


async def test_realtime_endpoints_read_the_running_hub(app, client, accounts, monkeypatch):
    # Spikes are rare by design (about 0.03% of readings); make them common so alerts exist.
    monkeypatch.setattr(realtime_service, "SPIKE_PROBABILITY", 0.5)
    hub = app.state.hub
    hub.recent.clear()
    hub.recent_alerts.clear()
    for _ in range(30):
        hub.tick()
    headers = accounts.viewer.headers
    latest = (await client.get("/api/v1/realtime/latest", params={"limit": 4, "server": "S0002"}, headers=headers)).json()
    assert len(latest) == 4 and {r["server"] for r in latest} == {"S0002"} and latest[0]["generated_at"].endswith("Z")
    gpu3 = (await client.get("/api/v1/realtime/latest", params={"server": "S0002", "sensor": "GPU3"}, headers=headers)).json()
    assert [(r["server"], r["category"], r["sensor"]) for r in gpu3] == [("S0002", "gpu_temp", "GPU3")]
    summary = (await client.get("/api/v1/realtime/summary", params={"seconds": 10}, headers=headers)).json()
    assert len(summary) == 10 and summary[-1]["type"] == "tick" and set(summary[-1]["summary"]) == set(hub.thresholds)
    alerts = (await client.get("/api/v1/realtime/alerts", params={"limit": 500}, headers=headers)).json()
    assert alerts and all(a["is_anomaly"] for a in alerts)
    assert all(a["value"] < a["threshold"] if a["category"] == "fan_rpm" else a["value"] > a["threshold"] for a in alerts)
    config = (await client.get("/api/v1/realtime/config", headers=headers)).json()
    assert config["servers"] == len(hub.servers)
    assert config["readings_per_second"] == len(hub.servers) * 50
    assert config["sensors"]["gpu_temp"] == [f"GPU{i}" for i in range(8)] and set(config["sensors"]) == set(hub.thresholds)


async def test_websocket_takes_the_token_from_a_header(app, live_server, accounts):
    headers = {"Authorization": accounts.viewer.headers["Authorization"]}
    async with websockets.connect(live_server, additional_headers=headers) as ws:  # no ?token= in the URL
        for _ in range(50):
            if app.state.hub.broadcaster.subscribers:
                break
            await asyncio.sleep(0.02)
        app.state.hub.tick()
        assert json.loads(await asyncio.wait_for(ws.recv(), timeout=2))["type"] == "tick"


async def test_websocket_pushes_readings(app, live_server, accounts):
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        for _ in range(50):  # wait until the endpoint has subscribed
            if app.state.hub.broadcaster.subscribers:
                break
            await asyncio.sleep(0.02)
        app.state.hub.tick()
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
    assert message["type"] == "tick"
    assert {"generated_at", "servers", "readings", "summary", "alerts", "hottest"} <= set(message)
    assert len(message["server_readings"]) == len(app.state.hub.servers) * len(app.state.hub.thresholds)
    for _ in range(50):
        if not app.state.hub.broadcaster.subscribers:
            break
        await asyncio.sleep(0.02)
    assert app.state.hub.broadcaster.subscribers == 0


def database_down(*_args, **_kwargs):
    from asyncmy.errors import OperationalError as DriverError
    from sqlalchemy.exc import OperationalError

    # What the driver raises for a lost connection: MariaDB client error 2006.
    raise OperationalError("SELECT 1", {}, DriverError(2006, "MySQL server has gone away"))


async def test_websocket_asks_to_retry_while_the_database_is_down(live_server, accounts, monkeypatch):
    monkeypatch.setattr(realtime_api, "load_active_user", database_down)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        with pytest.raises(websockets.ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=2)
    # 1013 "try again later", not 1008 or HTTP 403, so the client reconnects instead of logging out.
    assert closed.value.rcvd.code == 1013


async def test_websocket_keeps_streaming_when_a_recheck_hits_a_database_outage(app, live_server, accounts, monkeypatch):
    real_check = realtime_api.load_active_user
    checks = 0

    async def flaky(session, token):
        nonlocal checks
        checks += 1
        if checks == 1:
            return await real_check(session, token)
        database_down()

    monkeypatch.setattr(realtime_api, "load_active_user", flaky)
    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(realtime_api, "AUTH_RETRY_SECONDS", 0.05)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        for _ in range(50):
            if app.state.hub.broadcaster.subscribers:
                break
            await asyncio.sleep(0.02)
        received = 0
        for _ in range(4):
            await asyncio.sleep(0.1)  # let a re-check fail between ticks
            app.state.hub.tick()
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            received += message["type"] == "tick"
    assert received == 4 and checks >= 3


async def test_websocket_keeps_streaming_through_successful_rechecks(app, live_server, accounts, monkeypatch):
    real_check, checks = realtime_api.load_active_user, []

    async def counting(session, token):
        checks.append(1)
        return await real_check(session, token)

    monkeypatch.setattr(realtime_api, "load_active_user", counting)
    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.05)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        for _ in range(50):
            if app.state.hub.broadcaster.subscribers:
                break
            await asyncio.sleep(0.02)
        for _ in range(3):
            await asyncio.sleep(0.1)
            app.state.hub.tick()
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=2))["type"] == "tick"
    assert len(checks) >= 3  # the connect check plus re-checks that passed


async def test_a_hung_recheck_does_not_hold_back_messages(app, live_server, accounts, monkeypatch):
    real_check, calls = realtime_api.load_active_user, []

    async def hangs_after_connect(session, token):
        calls.append(1)
        if len(calls) == 1:
            return await real_check(session, token)
        await asyncio.Event().wait()  # a database that accepted the connection and went silent

    monkeypatch.setattr(realtime_api, "load_active_user", hangs_after_connect)
    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.05)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        for _ in range(50):
            if app.state.hub.broadcaster.subscribers:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # the re-check is now stuck
        for _ in range(3):
            app.state.hub.tick()
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=1))["type"] == "tick"
    assert len(calls) == 2


async def test_stopping_the_hub_stores_what_is_still_buffered():
    hub = RealtimeHub(fleet(4), seed=2)
    hub.start()
    for _ in range(100):  # the generator starts once the lease is confirmed; the first tick runs at once then
        await asyncio.sleep(0.02)
        if hub.recent:
            break
    assert hub.generating and hub.recent
    await hub.stop()
    async with SessionLocal() as session:
        stored = await session.scalar(select(func.count()).select_from(RealtimeReading))
        minutes = await session.scalar(select(func.sum(RealtimeMinuteStat.count)))
    assert stored >= 200 and stored % 200 == 0 and minutes == stored  # 4 servers x 50 sensors per tick


async def test_generator_lease_lets_one_process_run_the_fleet():
    from app.services import lease

    now = utcnow()
    assert await lease.claim("instance-a", now)
    assert not await lease.claim("instance-b", now + timedelta(seconds=1))  # a is alive
    assert await lease.claim("instance-a", now + timedelta(seconds=5))  # a renews
    silent = now + timedelta(seconds=5 + lease.TTL_SECONDS + 1)
    assert await lease.claim("instance-b", silent)  # a went silent: b takes over
    assert not await lease.claim("instance-a", silent + timedelta(seconds=1))
    await lease.release("instance-b")  # a clean stop hands it on at once
    assert await lease.claim("instance-a", silent + timedelta(seconds=2))


async def test_hub_keeps_its_generator_off_while_another_process_holds_the_lease(monkeypatch, caplog):
    held_elsewhere = True

    async def claim(_holder, _now):
        return not held_elsewhere

    monkeypatch.setattr(realtime_service.lease, "RENEW_SECONDS", 0.05)
    monkeypatch.setattr(realtime_service.lease, "claim", claim)
    hub = RealtimeHub(fleet(2), seed=5)
    with caplog.at_level(logging.ERROR, logger="app.realtime"):
        hub.start()
        await asyncio.sleep(0.15)
        assert not hub.generating and not hub.recent and "Another process runs the telemetry generator" in caplog.text
        held_elsewhere = False
        await asyncio.sleep(0.15)
        assert hub.generating and hub.recent  # took over once the lease was free
    await hub.stop()
    assert not hub.generating


async def test_hub_stops_its_generator_when_the_lease_cannot_be_renewed_within_the_ttl(monkeypatch, caplog):
    database_down = False

    async def claim(_holder, _now):
        if database_down:
            raise ConnectionRefusedError("database down")
        return True

    monkeypatch.setattr(realtime_service.lease, "RENEW_SECONDS", 0.05)
    monkeypatch.setattr(realtime_service.lease, "TTL_SECONDS", 0.3)
    monkeypatch.setattr(realtime_service.lease, "claim", claim)
    hub = RealtimeHub(fleet(2), seed=5)
    with caplog.at_level(logging.ERROR, logger="app.realtime"):
        hub.start()
        await asyncio.sleep(0.1)
        assert hub.generating
        database_down = True
        await asyncio.sleep(0.15)
        assert hub.generating  # within the TTL of the last confirmed claim: still ours
        await asyncio.sleep(0.4)
        assert not hub.generating and "could not be renewed" in caplog.text  # past it: another process may hold it
        database_down = False
        await asyncio.sleep(0.15)
        assert hub.generating  # confirmed again
    await hub.stop()
    # A first start that gets no answer does not guess.
    database_down = True
    silent = RealtimeHub(fleet(2), seed=5)
    silent.start()
    await asyncio.sleep(0.15)
    assert not silent.generating and not silent.recent
    await silent.stop()


async def test_a_bad_reading_is_dropped_alone():
    hub = RealtimeHub(fleet(2), seed=4)
    hub.tick()
    good = hub.last_readings
    bad = dict(good[0], server="S" * 50, category="cpu_temp")  # longer than the column allows
    hub.writer.extend([bad])
    assert await hub.writer.flush() == len(good)
    assert hub.writer.dropped == 1 and hub.writer.pending == 0
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RealtimeReading)) == len(good)


async def test_websocket_rejects_invalid_token(live_server):
    with pytest.raises(websockets.InvalidStatus) as exc:
        async with websockets.connect(f"{live_server}?token=not-a-jwt"):
            pass
    assert exc.value.response.status_code == 403


async def test_websocket_closes_when_account_is_disabled(live_server, accounts, monkeypatch):
    from app.models import User
    from sqlalchemy import update

    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.1)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        async with SessionLocal() as session:
            await session.execute(update(User).where(User.id == accounts.viewer.user.id).values(is_active=False))
            await session.commit()
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=2)
    assert closed.value.rcvd.code == 1008


async def test_websocket_closes_after_the_user_logs_out(live_server, client, accounts, monkeypatch):
    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.1)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        assert (await client.post("/api/v1/auth/logout", headers=accounts.viewer.headers)).status_code == 204
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=2)
    assert closed.value.rcvd.code == 1008


async def test_a_failing_tick_does_not_stop_the_generator(caplog):
    hub = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 2, "realtime_interval_seconds": 0.01}), seed=3)
    calls = 0
    real_tick = hub.tick

    def flaky_tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bug in one tick")
        return real_tick()

    hub.tick = flaky_tick
    hub._lease_deadline = asyncio.get_running_loop().time() + 60
    task = asyncio.create_task(hub._run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if calls >= 3:
                break
    finally:
        task.cancel()
    assert calls >= 3 and len(hub.recent) >= 2
    assert "Real-time tick failed" in caplog.text


def test_summary_counts_seconds_not_ticks():
    hub = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 2, "realtime_interval_seconds": 2}), seed=4)
    for _ in range(10):
        hub.tick()
    assert len(hub.summaries(10)) == 5


async def test_purge_waits_while_the_writer_is_behind():
    old = utcnow() - timedelta(hours=48)
    async with SessionLocal() as session:
        session.add(RealtimeReading(server="S0001", category="cpu_temp", sensor="CPU0", value=1, threshold=90, is_anomaly=False,
                                    generated_at=old))
        await session.commit()
    behind = True
    purge = asyncio.create_task(purge_before(RealtimeReading, RealtimeReading.generated_at, utcnow(), busy=lambda: behind))
    await asyncio.sleep(0.3)
    assert not purge.done()
    behind = False
    assert await asyncio.wait_for(purge, 3) == 1


async def test_purge_stops_waiting_for_a_writer_that_stays_behind(monkeypatch, caplog):
    monkeypatch.setattr(retention_service, "MAX_BUSY_WAIT_SECONDS", 1)
    old = utcnow() - timedelta(hours=48)
    async with SessionLocal() as session:
        session.add(RealtimeReading(server="S0001", category="cpu_temp", sensor="CPU0", value=1, threshold=90, is_anomaly=False,
                                    generated_at=old))
        await session.commit()
    with caplog.at_level("WARNING", logger="app.retention"):
        purge = purge_before(RealtimeReading, RealtimeReading.generated_at, utcnow(), busy=lambda: True)
        assert await asyncio.wait_for(purge, 5) == 1
    assert "deleting anyway" in caplog.text


async def test_a_waiting_readings_purge_does_not_hold_back_the_other_tables():
    async with SessionLocal() as session:
        session.add(SystemLog(created_at=utcnow() - timedelta(days=60), level="INFO", logger="app.test", message="old"))
        await session.commit()
    run = asyncio.create_task(RetentionJob(get_settings(), busy=lambda: True).run_once())
    try:
        for _ in range(50):
            await asyncio.sleep(0.05)
            async with SessionLocal() as session:
                if not await session.scalar(select(func.count()).select_from(SystemLog).where(SystemLog.message == "old")):
                    break
        else:
            raise AssertionError("old system log was not purged while the readings purge waited")
    finally:
        run.cancel()


async def test_a_held_up_loop_is_counted_as_skipped_seconds(caplog):
    import time

    hub = RealtimeHub(fleet(1), seed=3)
    hub.interval = 0.1
    hub._lease_deadline = asyncio.get_running_loop().time() + 60
    task = asyncio.create_task(hub._run())
    await asyncio.sleep(0.05)
    time.sleep(0.45)  # something blocks the event loop for more than four intervals
    await asyncio.sleep(0.15)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert hub.skipped_ticks >= 3
    assert any("ticks skipped" in record.getMessage() for record in caplog.records)
