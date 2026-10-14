"""Simulated burn-in fleet: generate, summarise, broadcast once per tick, write in batches.

Every tick produces one reading per server and sensor. A server has several sensors per
category (8 GPU temperatures, 16 memory temperatures, ...). Consumers get what they need
at their own scale:
- the database gets every raw reading through a batch writer;
- dashboards get one message per tick (summary, alerts, hottest sensors, and for fleets
  up to one line the hottest sensor of each server and category), so the WebSocket cost
  stays bounded;
- analytics read per-minute statistics per category instead of scanning raw readings.
"""

import asyncio
import contextlib
import heapq
import json
import logging
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.core.time import utcnow
from app.db.session import DUPLICATE_KEY, SessionLocal, error_code, is_rejected_row
from app.models.lease import GeneratorLease
from app.models.reading import RealtimeMinuteStat, RealtimeReading
from app.schemas.analytics import Interval
from app.services import lease, site_settings
from app.services.alerts import write_alerts
from app.services.analytics import BUCKET_FORMATS
from app.services.batch import BatchWriter

logger = logging.getLogger("app.realtime")

# The sensors of one simulated server, per category: 50 with the default categories, about
# what a GPU server's BMC reports. A configured category that is not listed has one sensor.
SENSORS: dict[str, tuple[str, ...]] = {
    "chamber_temp": ("Inlet", "Panel"),
    "cpu_temp": ("CPU0", "CPU1"),
    "gpu_temp": tuple(f"GPU{i}" for i in range(8)),
    "dimm_temp": tuple(f"DIMM{i:02d}" for i in range(16)),
    "nvme_temp": tuple(f"NVMe{i}" for i in range(8)),
    "fan_rpm": tuple(f"Fan{i}" for i in range(10)),
    "power_w": tuple(f"PSU{i}" for i in range(4)),
}
DEFAULT_SENSORS = ("Main",)

# Simulated readings, demo values only. Each metric's spread is a share of its .env threshold,
# and its mean sits 6 spreads on the safe side of it (below, or above for a metric that alerts
# when low). Normal noise then stays below the default 90% warning level even for the hottest of 320
# memory modules, so a warning means something; the spikes below start on about 0.03% of readings,
# one every few seconds at 20 servers (1,000 readings/s), and each lasts a few seconds. With the default thresholds
# the means are 31.5 °C inlet, 60 °C CPU, 60 °C GPU and memory, 49 °C SSD, 11,100 RPM and
# 1,550 W per power supply. An alert then stands out instead of the dashboard being red all the time. The
# values follow the .env threshold, not the one an admin sets later: raising a limit must mean
# fewer alerts, not readings that move with it.
SPREAD_SHARE: dict[str, float] = {"cpu_temp": 0.056, "power_w": 0.071, "fan_rpm": 0.45}
DEFAULT_SPREAD_SHARE = 0.05
SPIKE_PROBABILITY = 0.0003
# Once a sensor spikes it stays past its baseline for this many further ticks (seconds), chosen at
# random: an alert then has a duration and a peak, as a real excursion does.
EXCURSION_TICKS = (2, 19)
# Debounce: a sensor must be past its limit for this many consecutive readings (seconds) before its
# alert is stored as an event. A single noisy reading is still flagged on the reading and in the
# live alert list, but does not become an event someone has to acknowledge.
ALERT_MIN_READINGS = 3
MAX_ALERTS_PER_TICK = 50
HOTTEST_PER_TICK = 10
RECENT_ALERTS = 500
# Ticks of up to one burn-in line (200 servers) also push, per server and category, the
# sensor nearest its limit. Larger fleets push the summary alone, so the message stays bounded.
SERVER_DETAIL_SERVERS = 200
SETTINGS_SYNC_SECONDS = 30
# Rows per INSERT and transaction. The driver builds each statement synchronously, so one
# statement for a large backlog would hold the event loop for seconds and delay the ticks.
WRITE_CHUNK = 5_000


def server_ids(count: int) -> list[str]:
    return [f"S{i:04d}" for i in range(1, count + 1)]


def generate_value(category: str, baseline: float, rng: random.Random, below: bool = False, spike: bool = False) -> float:
    """One reading: noise on the safe side of the baseline, or a spike past it (`spike`, or by chance)."""
    sd = baseline * SPREAD_SHARE.get(category, DEFAULT_SPREAD_SHARE)
    side = 1 if below else -1  # which side of the limit is safe
    value = rng.gauss(baseline + side * 6 * sd, sd)
    if spike or rng.random() < SPIKE_PROBABILITY:
        # Occasional excursion so the alert path is visible in a short demo. A slow fan still
        # turns: at least a fifth of its limit, so "% of limit" stays readable (at most 500%).
        value = baseline - side * abs(rng.gauss(0, sd))
        if below:
            value = max(value, baseline / 5)
    return round(max(value, 0.0), 2)


def load(value: float, threshold: float, below: bool) -> float:
    """How close a reading is to its limit, in percent: 100 is the limit, above 100 an alert."""
    if below:
        return threshold / value * 100 if value > 0 else float("inf")
    return value / threshold * 100


@dataclass(slots=True)
class CategoryStats:
    count: int = 0
    total: float = 0.0
    minimum: float = float("inf")
    maximum: float = float("-inf")
    anomalies: int = 0
    max_at: tuple[str, str] = ("", "")  # server and sensor of the maximum
    min_at: tuple[str, str] = ("", "")

    def add(self, value: float, server: str, sensor: str, anomaly: bool) -> None:
        self.count += 1
        self.total += value
        if value < self.minimum:
            self.minimum, self.min_at = value, (server, sensor)
        if value > self.maximum:
            self.maximum, self.max_at = value, (server, sensor)
        self.anomalies += anomaly

    def merge(self, other: "CategoryStats") -> None:
        self.count += other.count
        self.total += other.total
        if other.minimum < self.minimum:
            self.minimum, self.min_at = other.minimum, other.min_at
        if other.maximum > self.maximum:
            self.maximum, self.max_at = other.maximum, other.max_at
        self.anomalies += other.anomalies


@dataclass(slots=True)
class MinuteAccumulator:
    minute: datetime | None = None
    stats: dict[str, CategoryStats] = field(default_factory=dict)
    contribution_id: str = field(default_factory=lambda: uuid4().hex)

    def rows(self) -> list[dict]:
        return [
            {"minute": self.minute, "category": c, "contribution_id": self.contribution_id, "count": s.count, "total": s.total,
             "minimum": s.minimum, "maximum": s.maximum, "anomalies": s.anomalies}
            for c, s in self.stats.items()
            if s.count
        ]


def _wire(reading: dict) -> dict:
    """A reading as sent to clients: ISO time, no database-only fields."""
    return reading | {"generated_at": reading["generated_at"].isoformat() + "Z"}


class Broadcaster:
    """In-process fan-out of JSON text. Each subscriber has a bounded queue; slow clients lose old messages.

    Messages are serialised once per tick, not once per connection.
    """

    def __init__(self, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[str]] = set()

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: str) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)


def _reading_key(reading: dict) -> tuple:
    return reading["server"], reading["generated_at"], reading["category"], reading["sensor"]


async def write_readings(batch: list[dict]) -> None:
    """ORM bulk INSERT of one chunk, safe to retry.

    If a commit succeeded but its reply was lost, the retry hits the unique
    (server, generated_at, category, sensor) key. The chunk is then written again with only
    the readings that are not stored yet.
    """
    async with SessionLocal() as session:
        try:
            await session.execute(insert(RealtimeReading), batch)
            await session.commit()
            return
        except IntegrityError as exc:
            if error_code(exc) != DUPLICATE_KEY:
                raise
            await session.rollback()
        servers = {r["server"] for r in batch}
        times = {r["generated_at"] for r in batch}
        result = await session.execute(
            select(RealtimeReading.server, RealtimeReading.generated_at, RealtimeReading.category, RealtimeReading.sensor)
            .where(RealtimeReading.server.in_(servers), RealtimeReading.generated_at.in_(times))
        )
        stored = {tuple(row) for row in result}
        missing = [r for r in batch if _reading_key(r) not in stored]
        if missing:
            await session.execute(insert(RealtimeReading), missing)
        await session.commit()


async def write_minute_stats(batch: list[dict]) -> None:
    """Apply each accumulator contribution once, including retries after an ambiguous commit.

    A restarted or partially flushed accumulator has a new ID, even if its values match.
    Its receipt and delta commit together; retries retain the ID across batch/chunk changes.
    Startup backfill replaces numeric values from raw rows but preserves these receipts.
    """
    async with SessionLocal() as session:
        for row in batch:
            contribution_id = row["contribution_id"]
            stored = await session.get(RealtimeMinuteStat, (row["minute"], row["category"]), with_for_update=True)
            if stored is None:
                values = {name: value for name, value in row.items() if name != "contribution_id"}
                session.add(RealtimeMinuteStat(**values, applied_contributions=[contribution_id]))
                continue
            received = stored.applied_contributions or []
            if contribution_id in received:
                continue
            stored.count += row["count"]
            stored.total += row["total"]
            stored.minimum = min(stored.minimum, row["minimum"])
            stored.maximum = max(stored.maximum, row["maximum"])
            stored.anomalies += row["anomalies"]
            stored.applied_contributions = [*received, contribution_id]
        await session.commit()


def _report_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Real-time task %s stopped: %r", task.get_name(), task.exception())


BACKFILL_HOURS = 2


async def backfill_minute_stats(started_at: datetime, *, holder: str | None = None) -> int:
    """Rebuild the per-minute statistics from the raw readings stored before this start.

    Recompute the last two hours, capped by raw retention and starting at a whole minute.
    A later stored bucket must not hide earlier missing or replay-corrupted buckets. Raw
    readings committed after this scan need another start within this bounded window;
    dropped/expired raw readings cannot be recovered. Receipt IDs survive numeric repair.
    Exclude the startup minute entirely. With a holder, verify its current lease
    under a row lock in the repair transaction, after the expensive raw scan.
    Returns the number of rows written. Standalone tests may omit the holder.
    """
    sql_format, py_format = BUCKET_FORMATS[Interval.minute]
    minute = func.date_format(RealtimeReading.generated_at, sql_format).label("minute")
    async with SessionLocal() as session:
        hours = min(BACKFILL_HOURS, get_settings().realtime_retention_hours)
        cutoff = started_at - timedelta(hours=hours)
        since = cutoff.replace(second=0, microsecond=0)
        if since < cutoff:
            since += timedelta(minutes=1)
        query = (
            select(
                minute, RealtimeReading.category,
                func.count().label("count"), func.sum(RealtimeReading.value).label("total"),
                func.min(RealtimeReading.value).label("minimum"), func.max(RealtimeReading.value).label("maximum"),
                func.sum(RealtimeReading.is_anomaly).label("anomalies"),
            )
            .where(RealtimeReading.generated_at >= since,
                   RealtimeReading.generated_at < started_at.replace(second=0, microsecond=0))
            .group_by(minute, RealtimeReading.category)
        )
        rows = (await session.execute(query)).all()
        await session.commit()  # release the scan snapshot before locking current ownership
        if holder is not None:
            ownership = await session.get(GeneratorLease, lease.NAME, with_for_update=True)
            if (ownership is None or ownership.holder != holder
                    or ownership.renewed_at <= utcnow() - timedelta(seconds=lease.TTL_SECONDS)):
                return 0
        for r in rows:
            values = {"count": int(r._mapping["count"]), "total": float(r.total), "minimum": float(r.minimum),
                      "maximum": float(r.maximum), "anomalies": int(r.anomalies)}
            key = (datetime.strptime(r.minute, py_format), r.category)
            stored = await session.get(RealtimeMinuteStat, key)
            if stored is None:
                session.add(RealtimeMinuteStat(minute=key[0], category=key[1], **values))
            else:
                for name, value in values.items():
                    setattr(stored, name, value)
        await session.commit()
    return len(rows)


class RealtimeHub:
    def __init__(self, settings: Settings, *, seed: int | None = None) -> None:
        self.interval = settings.realtime_interval_seconds
        # The .env thresholds shape the simulated values; the current ones, which an admin can
        # change, decide what is an alert.
        self.baseline = dict(settings.realtime_thresholds)
        self.thresholds = dict(settings.realtime_thresholds)
        self.below = {category for category in self.thresholds if category in settings.realtime_alert_below}
        self.warning_percent = settings.realtime_warning_percent
        self.sensors = {category: SENSORS.get(category, DEFAULT_SENSORS) for category in self.thresholds}
        self.servers = server_ids(settings.realtime_servers)
        self.recent: deque[dict] = deque(maxlen=settings.realtime_recent_size)
        self.recent_alerts: deque[dict] = deque(maxlen=RECENT_ALERTS)
        self.last_readings: list[dict] = []
        self.broadcaster = Broadcaster()
        self.writer = BatchWriter(
            "realtime",
            write_readings,
            batch_size=settings.realtime_flush_batch_size,
            interval=settings.realtime_flush_interval_seconds,
            max_pending=settings.realtime_max_pending,
            is_permanent=is_rejected_row,
            chunk_size=WRITE_CHUNK,
            concurrency=settings.realtime_write_concurrency,
        )
        self.minute_writer = BatchWriter(
            "realtime_minutes",
            write_minute_stats,
            batch_size=max(1, len(self.thresholds)),
            interval=settings.realtime_flush_interval_seconds,
            max_pending=10_000,
            is_permanent=is_rejected_row,
        )
        # Alert events: one per server, category and sensor, from the first reading past the limit
        # to the first one back inside. Few per minute, written when they close.
        self.alert_writer = BatchWriter(
            "realtime_alerts",
            write_alerts,
            batch_size=100,
            interval=settings.realtime_flush_interval_seconds,
            max_pending=50_000,
            is_permanent=is_rejected_row,
            chunk_size=WRITE_CHUNK,  # a threshold change can close every sensor's alert in one tick
        )
        self._open_alerts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._excursions: dict[tuple[str, str, str], int] = {}  # sensors in a spike: ticks still to go
        self.short_alerts = 0  # alerts that ended before ALERT_MIN_READINGS: flagged readings only, no event
        self._minute = MinuteAccumulator()
        self._rng = random.Random(seed)
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._lease_task: asyncio.Task | None = None
        self._lease_watchdog_task: asyncio.Task | None = None
        self._lease_deadline: float | None = None
        self._lease_changed = asyncio.Event()
        self._generator_lock = asyncio.Lock()
        self._rebuild_task: asyncio.Task | None = None
        self._lease_refused = False  # another process holds the generator lease
        self.generating = False  # this process holds the generator lease and its generator runs
        self._settings_changed_at: datetime | None = None
        self.skipped_ticks = 0  # seconds without readings because the event loop was held up

    @property
    def readings_per_second(self) -> float:
        return len(self.servers) * self.sensors_per_server / self.interval

    @property
    def sensors_per_server(self) -> int:
        return sum(len(sensors) for sensors in self.sensors.values())

    def tick(self) -> dict:
        """Generate a reading per server and sensor; hand each consumer its share."""
        now = utcnow()
        stats = {category: CategoryStats() for category in self.thresholds}
        readings: list[dict] = []
        alerts: list[dict] = []
        closed: list[dict] = []  # alert events that ended this tick
        detail = len(self.servers) <= SERVER_DETAIL_SERVERS
        per_server: list[dict] = []  # the sensor nearest its limit, per server and category
        for server in self.servers:
            for category, threshold in self.thresholds.items():
                below, baseline = category in self.below, self.baseline[category]
                top: dict[str, Any] | None = None
                for sensor in self.sensors[category]:
                    key = (server, category, sensor)
                    left = self._excursions.get(key)
                    if left:
                        # A spike lasts a few seconds, as a real excursion does, so an alert has a
                        # beginning, an end and a peak instead of being one reading.
                        value = generate_value(category, baseline, self._rng, below, spike=True)
                        if left > 1:
                            self._excursions[key] = left - 1
                        else:
                            del self._excursions[key]
                    else:
                        value = generate_value(category, baseline, self._rng, below)
                        if value < baseline if below else value > baseline:  # a spike began (noise never gets here)
                            self._excursions[key] = self._rng.randint(*EXCURSION_TICKS)
                    anomaly = value < threshold if below else value > threshold
                    reading: dict[str, Any] = {"server": server, "category": category, "sensor": sensor, "value": value,
                                               "threshold": threshold, "is_anomaly": anomaly, "generated_at": now}
                    readings.append(reading)
                    stats[category].add(value, server, sensor, anomaly)
                    if anomaly:
                        if len(alerts) < MAX_ALERTS_PER_TICK:
                            alerts.append(_wire(reading))
                        event = self._open_alerts.get((server, category, sensor))
                        if event is None:
                            self._open_alerts[server, category, sensor] = {
                                "server": server, "category": category, "sensor": sensor, "started_at": now, "ended_at": None,
                                "threshold": threshold, "peak_value": value, "readings": 1,
                            }
                        else:
                            event["readings"] += 1
                            if value < event["peak_value"] if below else value > event["peak_value"]:
                                event["peak_value"] = value
                    elif self._open_alerts and (event := self._open_alerts.pop((server, category, sensor), None)) is not None:
                        event["ended_at"] = now
                        if event["readings"] >= ALERT_MIN_READINGS:
                            closed.append(event)
                        else:
                            self.short_alerts += 1
                    if top is None or (value < top["value"] if below else value > top["value"]):
                        top = reading
                if detail and top is not None:  # every category has at least one sensor
                    per_server.append(top)
        hottest = heapq.nlargest(
            HOTTEST_PER_TICK, readings, key=lambda r: load(r["value"], r["threshold"], r["category"] in self.below)
        )
        message = {
            "type": "tick",
            "generated_at": now.isoformat() + "Z",
            "servers": len(self.servers),
            "readings": len(readings),
            "warning_percent": self.warning_percent,
            "summary": {
                category: {
                    "count": s.count,
                    "average": round(s.total / s.count, 2),
                    "minimum": s.minimum,
                    "maximum": s.maximum,
                    "threshold": self.thresholds[category],
                    "anomalies": s.anomalies,
                    "alert_below": category in self.below,
                    # The reading nearest the limit: the maximum, or the minimum for a metric that alerts when low.
                    "worst": s.minimum if category in self.below else s.maximum,
                    "worst_server": (s.min_at if category in self.below else s.max_at)[0],
                    "worst_sensor": (s.min_at if category in self.below else s.max_at)[1],
                }
                for category, s in stats.items()
            },
            "alerts": alerts,
            "hottest": [_wire(r) for r in hottest],
        }
        # Late joiners refill charts from `recent`, which keeps the summary only.
        self.recent.append(message)
        self.recent_alerts.extend(alerts)
        self.last_readings = readings
        if detail:
            # Compact: time and threshold are in the tick and its summary, not repeated 1,400 times.
            message = message | {"server_readings": [
                {"server": r["server"], "category": r["category"], "sensor": r["sensor"], "value": r["value"]} for r in per_server
            ]}
        self.broadcaster.publish(json.dumps(message))
        self.writer.extend(readings)
        if closed:
            self.alert_writer.extend(closed)
        self._accumulate(now, stats)
        return message

    def apply_settings(self, thresholds: dict[str, float], warning_percent: int, changed_at: datetime | None = None) -> None:
        """Use an admin's thresholds and warning level from the next tick on.

        `changed_at` is when they were saved. A poll that read the table just before a save
        must not put the older values back, so settings older than the applied ones are ignored;
        so are undated ones (read before the first save created the row) once a save was applied.
        """
        applied = self._settings_changed_at
        if applied is not None and (changed_at is None or changed_at < applied):
            return
        self.thresholds = {category: thresholds.get(category, value) for category, value in self.thresholds.items()}
        self.warning_percent = warning_percent
        self._settings_changed_at = changed_at or self._settings_changed_at

    async def sync_settings(self) -> None:
        async with SessionLocal() as session:
            current = await site_settings.load(session, self._settings)
        self.apply_settings(current.thresholds, current.warning_percent, current.updated_at)

    async def _sync(self) -> None:
        """Read the settings at start and then every SETTINGS_SYNC_SECONDS.

        A change through this API applies at once; the poll catches a change made through
        another API instance, and a start while the database was down.
        """
        failing = False
        while True:
            try:
                await self.sync_settings()
                failing = False
            except Exception as exc:  # noqa: BLE001 - keep the last thresholds and try again
                if not failing:
                    logger.warning("Could not read the alert settings; keeping the current ones: %s", type(exc).__name__)
                failing = True
            await asyncio.sleep(SETTINGS_SYNC_SECONDS)

    def _accumulate(self, now: datetime, stats: dict[str, CategoryStats]) -> None:
        minute = now.replace(second=0, microsecond=0)
        if self._minute.minute is not None and minute != self._minute.minute:
            self.minute_writer.extend(self._minute.rows())
            self._minute = MinuteAccumulator()
        self._minute.minute = minute
        for category, s in stats.items():
            self._minute.stats.setdefault(category, CategoryStats()).merge(s)

    def flush_minute(self) -> None:
        """Queue the current, partial minute; used on shutdown."""
        if self._minute.minute is not None:
            self.minute_writer.extend(self._minute.rows())
            self._minute = MinuteAccumulator()

    def close_alerts(self, ended_at: datetime) -> None:
        """Close the alerts still open and queue them: the generator stops (a shutdown, or the lease
        went to another process), so nothing will see those sensors come back inside their limit."""
        if self._open_alerts:
            lasting = [event for event in self._open_alerts.values() if event["readings"] >= ALERT_MIN_READINGS]
            for event in lasting:
                event["ended_at"] = ended_at
            self.short_alerts += len(self._open_alerts) - len(lasting)
            self.alert_writer.extend(lasting)
            self._open_alerts = {}

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        last_failure_log = last_skip_log = float("-inf")
        while True:
            # After an event-loop stall the next tick must not precede the watchdog
            # and emit readings under an expired lease.
            if self._lease_deadline is None or loop.time() >= self._lease_deadline:
                await asyncio.sleep(min(self.interval, lease.RENEW_SECONDS))
                continue
            try:
                self.tick()
            except Exception:
                # Keep the stream alive; a bug in one tick must not freeze every chart. Log at
                # most once a minute so a repeating failure does not flood the system log.
                if loop.time() - last_failure_log >= 60:
                    last_failure_log = loop.time()
                    logger.exception("Real-time tick failed; skipping it")
            next_tick += self.interval
            # If the loop was held up past a whole interval, skip ahead instead of bursting to
            # catch up. Those seconds have no readings, so they are counted and logged.
            behind = loop.time() - next_tick
            if behind >= self.interval:
                skipped = int(behind // self.interval)
                self.skipped_ticks += skipped
                if loop.time() - last_skip_log >= 60:
                    last_skip_log = loop.time()
                    logger.warning("Generator held up %.1f s: %d ticks skipped (%d in total)", behind, skipped, self.skipped_ticks)
                next_tick += skipped * self.interval
            await asyncio.sleep(max(0.0, next_tick - loop.time()))

    async def _keep_lease(self) -> None:
        """Run the generator only while this process holds the generator lease.

        Claimed at start and renewed every `lease.RENEW_SECONDS`. Refused (another process
        renewed it recently), the generator stays off and the claim is retried, so a successor
        takes over once the holder stops or goes silent. Fail closed: while the database does
        not answer, the generator runs only until its last confirmed claim is `TTL_SECONDS` old,
        because from then on another process may hold the lease; it resumes with the next
        confirmed claim. A first start waits for an answer rather than guessing.
        """
        loop = asyncio.get_running_loop()
        while True:
            requested_at, requested_utc = loop.time(), utcnow()
            try:
                granted = await asyncio.wait_for(
                    lease.claim(lease.HOLDER, requested_utc), timeout=min(lease.RENEW_SECONDS, lease.TTL_SECONDS),
                )
            except Exception as exc:  # noqa: BLE001 - the database did not answer
                within_ttl = self._lease_deadline is not None and loop.time() < self._lease_deadline
                granted = self.generating and within_ttl
                if self.generating and not granted:
                    logger.error("The generator lease could not be renewed for %d s (%s); stopping the generator until the "
                                 "database confirms it again, so a successor cannot run beside it", lease.TTL_SECONDS,
                                 type(exc).__name__)
                else:
                    logger.warning("Could not check the generator lease: %s", type(exc).__name__)
            else:
                if granted:
                    # The database stores requested_utc, not the reply time. A slow
                    # successful reply cannot extend local authority past that TTL.
                    deadline = requested_at + lease.TTL_SECONDS
                    granted = loop.time() < deadline
                    if granted:
                        self._lease_deadline = deadline
                        self._lease_changed.set()
                elif not self._lease_refused:
                    logger.error("Another process runs the telemetry generator for this database (lease %r held elsewhere); "
                                 "this one keeps its generator off and will take over if that holder stops", lease.NAME)
                self._lease_refused = not granted
            async with self._generator_lock:
                if granted:
                    if self._rebuild_task is None:
                        self._rebuild_task = asyncio.create_task(
                            backfill_minute_stats(requested_utc, holder=lease.HOLDER), name="minute-stats-rebuild",
                        )
                        self._rebuild_task.add_done_callback(_report_crash)
                    self._start_generator()
                else:
                    self._lease_deadline = None
                    self._lease_changed.set()
                    await self._stop_generator()
            await asyncio.sleep(lease.RENEW_SECONDS)

    async def _watch_lease(self) -> None:
        """Expiry remains independent of a blocked renewal or its cancellation cleanup."""
        loop = asyncio.get_running_loop()
        while True:
            self._lease_changed.clear()
            deadline = self._lease_deadline
            if deadline is None:
                await self._lease_changed.wait()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._lease_changed.wait(), timeout=max(0, deadline - loop.time()))
            async with self._generator_lock:
                if self._lease_deadline is not None and loop.time() >= self._lease_deadline:
                    self._lease_deadline = None
                    logger.error("The generator lease could not be renewed before its TTL; stopping the generator")
                    await self._stop_generator()

    def _start_generator(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="realtime-generator")
            self._task.add_done_callback(_report_crash)
            self.generating = True
            logger.info(
                "Real-time generator started: %d servers x %d sensors in %d categories = %.0f readings/s",
                len(self.servers), self.sensors_per_server, len(self.thresholds), self.readings_per_second,
            )

    async def _stop_generator(self) -> None:
        """Stop the tick loop and hand its memory state to the writers: the partial minute, and the
        open alerts closed now, since this process will not see those sensors recover."""
        task, self._task = self._task, None
        self.generating = False  # authority ends before awaiting task cancellation
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the caller is being cancelled too (a shutdown): that must go through
            self.flush_minute()
            self.close_alerts(utcnow())

    def start(self) -> None:
        self.writer.start()
        self.minute_writer.start()
        self.alert_writer.start()
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(self._sync(), name="realtime-settings")
            self._sync_task.add_done_callback(_report_crash)
        if self._lease_task is None:
            self._lease_task = asyncio.create_task(self._keep_lease(), name="realtime-lease")
            self._lease_task.add_done_callback(_report_crash)
        if self._lease_watchdog_task is None:
            self._lease_watchdog_task = asyncio.create_task(self._watch_lease(), name="realtime-lease-watchdog")
            self._lease_watchdog_task.add_done_callback(_report_crash)

    async def stop(self) -> None:
        for name in ("_sync_task", "_lease_task", "_lease_watchdog_task", "_rebuild_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # recovery failures were logged by its done callback
                    if name != "_rebuild_task":
                        raise
                setattr(self, name, None)
        held = self.generating
        await self._stop_generator()
        self.flush_minute()
        self.close_alerts(utcnow())
        # Side by side: each writer's stop has its own time limits, and the container's grace period
        # covers one of them comfortably, not three in a row with a slow database.
        await asyncio.gather(self.writer.stop(), self.minute_writer.stop(), self.alert_writer.stop())
        if held:
            with contextlib.suppress(Exception):  # the database may be down; the lease then expires by itself
                await asyncio.wait_for(lease.release(lease.HOLDER), timeout=5)

    def latest(
        self, limit: int, category: str | None = None, server: str | None = None, sensor: str | None = None
    ) -> list[dict]:
        items = [
            r for r in self.last_readings
            if (category is None or r["category"] == category.lower())
            and (server is None or r["server"] == server)
            and (sensor is None or r["sensor"] == sensor)
        ]
        return items[:limit]

    def alerts(self, limit: int) -> list[dict]:
        return list(self.recent_alerts)[-limit:]

    def summaries(self, seconds: int) -> list[dict]:
        ticks = max(1, math.ceil(seconds / self.interval))
        return list(self.recent)[-ticks:]
