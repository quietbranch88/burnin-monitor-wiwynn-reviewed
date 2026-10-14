"""Aggregations over test records or real-time telemetry, expressed with the ORM.

Records are aggregated directly. Telemetry is aggregated from per-minute statistics,
so the cost does not grow with the number of servers; raw readings are only read
for the Excel data sheet, and for the statistics of one server, which the per-minute
statistics do not keep.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.time import to_storage, utcnow
from app.models.reading import RealtimeMinuteStat, RealtimeReading
from app.models.record import Record
from app.schemas.analytics import CategorySummary, Interval, Source, Summary, TrendPoint
from app.services import excel

MAX_TREND_POINTS = 5000
EXPORT_ROW_LIMIT = 50_000
EXPORT_CHUNK = 5_000

# MariaDB DATE_FORMAT patterns for each bucket size, and how to parse them back.
BUCKET_FORMATS = {
    Interval.minute: ("%Y-%m-%d %H:%i:00", "%Y-%m-%d %H:%M:%S"),
    Interval.hour: ("%Y-%m-%d %H:00:00", "%Y-%m-%d %H:%M:%S"),
    Interval.day: ("%Y-%m-%d", "%Y-%m-%d"),
}


def _value_aggregates(value: InstrumentedAttribute) -> tuple[ColumnElement, ...]:
    return (
        func.count(value).label("count"),
        func.sum(value).label("total"),
        func.avg(value).label("average"),
        func.min(value).label("minimum"),
        func.max(value).label("maximum"),
    )


def _stat_aggregates() -> tuple[ColumnElement, ...]:
    count = func.sum(RealtimeMinuteStat.count)
    total = func.sum(RealtimeMinuteStat.total)
    return (
        count.label("count"),
        total.label("total"),
        (total / count).label("average"),
        func.min(RealtimeMinuteStat.minimum).label("minimum"),
        func.max(RealtimeMinuteStat.maximum).label("maximum"),
    )


@dataclass(frozen=True)
class SourceDefinition:
    model: type  # table the aggregates read
    category: InstrumentedAttribute
    timestamp: InstrumentedAttribute
    aggregates: Callable[[], tuple[ColumnElement, ...]]
    rows_model: type  # table the Excel data sheet reads
    rows_category: InstrumentedAttribute
    rows_timestamp: InstrumentedAttribute


SOURCES = {
    Source.records: SourceDefinition(
        Record, Record.category, Record.recorded_at, lambda: _value_aggregates(Record.value),
        Record, Record.category, Record.recorded_at,
    ),
    Source.realtime: SourceDefinition(
        RealtimeMinuteStat, RealtimeMinuteStat.category, RealtimeMinuteStat.minute, _stat_aggregates,
        RealtimeReading, RealtimeReading.category, RealtimeReading.generated_at,
    ),
}
# One server's telemetry: the per-minute statistics cover the whole fleet, so these are
# aggregated from that server's raw readings (the unique index starts with server and time).
ONE_SERVER = SourceDefinition(
    RealtimeReading, RealtimeReading.category, RealtimeReading.generated_at, lambda: _value_aggregates(RealtimeReading.value),
    RealtimeReading, RealtimeReading.category, RealtimeReading.generated_at,
)


@dataclass(frozen=True)
class AnalyticsFilters:
    source: Source = Source.records
    start: datetime | None = None
    end: datetime | None = None
    category: str | None = None
    server: str | None = None

    @property
    def definition(self) -> SourceDefinition:
        if self.source == Source.realtime and self.server:
            return ONE_SERVER
        return SOURCES[self.source]

    def effective(self) -> "AnalyticsFilters":
        """The range the figures cover: as given for test records; whole minutes for live telemetry."""
        return _minute_window(self) if self.source == Source.realtime else self

    def apply(self, query: Select, *, category: InstrumentedAttribute, timestamp: InstrumentedAttribute, model: type) -> Select:
        if self.start is not None:
            query = query.where(timestamp >= to_storage(self.start))
        if self.end is not None:
            query = query.where(timestamp <= to_storage(self.end))
        if self.category:
            query = query.where(category == self.category.strip().lower())  # stored lower-case
        if self.server:
            # Record and RealtimeReading have a server column; the per-minute statistics never
            # meet a server filter, because `definition` picks ONE_SERVER for it.
            query = query.where(getattr(model, "server") == self.server)  # noqa: B009 - the models share no base with it
        return query

    def apply_aggregate(self, query: Select) -> Select:
        d = self.definition
        return self.effective().apply(query, category=d.category, timestamp=d.timestamp, model=d.model)


def _as_float(value: Any) -> float | None:
    return None if value is None else round(float(value), 4)


async def summary(session: AsyncSession, filters: AnalyticsFilters) -> Summary:
    d = filters.definition
    row = (await session.execute(filters.apply_aggregate(select(*d.aggregates()).select_from(d.model)))).one()
    return Summary(
        # `row.count` works (a column named count shadows tuple.count), but only at runtime: the mapping is explicit.
        count=int(row._mapping["count"] or 0),
        total=_as_float(row.total),
        average=_as_float(row.average),
        minimum=_as_float(row.minimum),
        maximum=_as_float(row.maximum),
    )


async def by_category(session: AsyncSession, filters: AnalyticsFilters) -> list[CategorySummary]:
    d = filters.definition
    query = (
        filters.apply_aggregate(select(d.category.label("category"), *d.aggregates()).select_from(d.model))
        .group_by(d.category)
        .order_by(d.category)
    )
    rows = (await session.execute(query)).all()
    return [
        CategorySummary(
            category=r.category,
            count=int(r._mapping["count"]),
            total=_as_float(r.total),
            average=_as_float(r.average),
            minimum=_as_float(r.minimum),
            maximum=_as_float(r.maximum),
        )
        for r in rows
    ]


def _offset_periods(start: datetime, end: datetime, zone: ZoneInfo) -> list[tuple[datetime, int]]:
    """UTC instants starting each constant-offset period, including seconds in old offsets.

    The pinned IANA data has no two transitions within a UTC day in the supported
    1970--2999 range (checked independently from TZif transition tables). Daily
    probes locate a changed day; binary search finds the exact transition second.
    Scan the actual data extent, with two days for bucket boundaries, not open
    user-supplied bounds. No database timezone tables or private zoneinfo APIs.
    """
    def offset(at: datetime) -> int:
        return int((at.replace(tzinfo=UTC).astimezone(zone).utcoffset() or timedelta()).total_seconds())

    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    end = end.replace(microsecond=0)
    periods = [(cursor, offset(cursor))]
    while cursor < end:
        following = min(cursor + timedelta(days=1), end)
        following_offset = offset(following)
        if following_offset != periods[-1][1]:
            low, high = cursor, following
            while (high - low).total_seconds() > 1:
                middle = low + timedelta(seconds=int((high - low).total_seconds()) // 2)
                if offset(middle) == periods[-1][1]:
                    low = middle
                else:
                    high = middle
            periods.append((high, following_offset))
        cursor = following
    return periods


async def trend(
    session: AsyncSession, filters: AnalyticsFilters, interval: Interval, zone: ZoneInfo | None = None
) -> list[TrendPoint]:
    """Historical local calendar buckets, returned as UTC; repeated hours stay distinct.

    At a fractional-hour transition a partial hour starts at the transition itself.
    Days combine both offsets and start at that date's local midnight.
    """
    zone = zone or ZoneInfo("UTC")
    d = filters.definition
    sql_format, py_format = BUCKET_FORMATS[interval]
    first, last = (await session.execute(filters.apply_aggregate(
        select(func.min(d.timestamp), func.max(d.timestamp)).select_from(d.model)
    ))).one()
    if first is None:
        return []
    # Run the bounded calendar scan outside the event loop for very long histories.
    periods = await asyncio.to_thread(_offset_periods, first, last, zone)
    offsets = case(*[(d.timestamp >= at, seconds) for at, seconds in reversed(periods[1:])],
                   else_=periods[0][1]) if len(periods) > 1 else periods[0][1]
    shifted = func.addtime(d.timestamp, func.sec_to_time(offsets))
    local_bucket = func.date_format(shifted, sql_format)
    if interval == Interval.day:
        bucket = local_bucket.label("bucket")
    else:
        # An offset rollback produces two different UTC buckets with the same local
        # label. Clamp shortened hours (e.g. Lord Howe's 30-minute DST change) to the
        # actual start of that offset period, so a bucket never precedes its period.
        period_start = case(*[(d.timestamp >= at, at) for at, _ in reversed(periods[1:])],
                            else_=periods[0][0]) if len(periods) > 1 else periods[0][0]
        nominal = func.subtime(func.str_to_date(local_bucket, "%Y-%m-%d %H:%i:%s"), func.sec_to_time(offsets))
        bucket = func.greatest(nominal, period_start).label("bucket")
    query = (
        filters.apply_aggregate(select(bucket, d.category.label("category"), *d.aggregates()).select_from(d.model))
        .group_by(bucket, d.category)
        # Newest first so the cap drops the oldest buckets, then restore time order.
        .order_by(bucket.desc(), d.category.desc())
        .limit(MAX_TREND_POINTS)
    )
    rows = list(reversed((await session.execute(query)).all()))
    return [
        TrendPoint(
            bucket=(datetime.strptime(r.bucket, py_format).replace(tzinfo=zone).astimezone(UTC)
                    if interval == Interval.day else r.bucket),
            category=r.category,
            count=int(r.count),
            average=round(float(r.average), 4),
            minimum=round(float(r.minimum), 4),
            maximum=round(float(r.maximum), 4),
        )
        for r in rows
    ]


def _minute_window(filters: AnalyticsFilters) -> AnalyticsFilters:
    """Live telemetry figures cover the whole minutes inside the range, up to the last finished minute.

    The fleet statistics are per minute, so a partial minute cannot be counted exactly; it is
    left out rather than counted whole, so no reading outside the range is counted. One
    server's figures come from its raw readings and use the same minutes, so the two agree,
    and the minute in progress, which has no statistics yet, is left out of both."""
    minute = timedelta(minutes=1)
    microsecond = timedelta(microseconds=1)
    start = None
    if filters.start is not None:
        start = to_storage(filters.start)
        floor = start.replace(second=0, microsecond=0)
        if floor.year == 9999 and floor.month == 12 and floor.day == 31 and floor.hour == 23 and floor.minute == 59:
            return replace(filters, start=datetime.max, end=datetime.min)  # no completed representable minute
        start = floor if floor == start else floor + minute
    end = utcnow().replace(second=0, microsecond=0) - microsecond  # the last finished minute ends here
    if filters.end is not None:
        # The end of the last minute that ends at or before the given end.
        requested = min(end, to_storage(filters.end))
        if requested < datetime.min + minute - microsecond:
            return replace(filters, start=datetime.max, end=datetime.min)
        end = (requested + microsecond).replace(second=0, microsecond=0) - microsecond
    return replace(filters, start=start, end=end)


async def export_rows(session: AsyncSession, filters: AnalyticsFilters, limit: int) -> list[object]:
    d = filters.definition
    filters = filters.effective()  # the Data sheet lists the minutes the statistics cover
    query = filters.apply(select(d.rows_model), category=d.rows_category, timestamp=d.rows_timestamp, model=d.rows_model)
    query = query.order_by(d.rows_timestamp.desc()).limit(limit)
    # Streamed in chunks with a pause between them: building 50,000 ORM objects in one go held
    # the event loop for seconds, and the generator lost those seconds of readings.
    rows: list[object] = []
    result = await session.stream_scalars(query)
    async for chunk in result.partitions(EXPORT_CHUNK):
        rows.extend(chunk)
        await asyncio.sleep(0)
    return rows



async def export_workbook(
    session: AsyncSession, filters: AnalyticsFilters, interval: Interval, zone: ZoneInfo | None = None
) -> tuple[bytes, str]:
    """Excel workbook with Summary, By category, Trend and Data sheets, and its file name."""
    zone = zone or ZoneInfo("UTC")
    def when(moment: datetime | None) -> str:
        # In the requested zone, to the second: the reader compares it with the Trend sheet.
        if moment is None:
            return "not set (open)"
        aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        try:
            local = aware.astimezone(zone).replace(tzinfo=None)
        except OverflowError:
            # A valid UTC boundary can fall beyond years 1--9999 in the display zone.
            # Preserve the actual bound and explicitly label the UTC representation.
            utc = aware.astimezone(UTC).replace(tzinfo=None)
            return f"{utc.isoformat(sep=' ', timespec='seconds')} (UTC; outside {zone.key} datetime range)"
        return f"{local.isoformat(sep=' ', timespec='seconds')} ({zone.key})"

    covered = filters.effective()
    empty_minutes = (filters.source == Source.realtime and covered.start is not None
                     and covered.end is not None and covered.start > covered.end)
    # Empty whole-minute windows use contradictory internal query bounds. Display
    # the requested bounds instead of leaking those sentinel dates into the report.
    displayed = filters if empty_minutes else covered
    described = {
        "Start": when(displayed.start),
        "End": when(displayed.end) if displayed.end is not None else "not set: up to the time of the report",
        "Category": filters.category or "all categories",
        "Server": filters.server or "all servers",
        "Trend interval": interval.value,
        "Trend time zone": zone.key,
    }
    if filters.source == Source.realtime:
        described["Range"] = ("empty: no whole minutes inside the requested range, up to the last finished minute"
                              if empty_minutes else "whole minutes inside the requested range, up to the last finished minute")
    build = partial(
        excel.build_workbook,
        source=filters.source,
        all_categories=not bool(filters.category),
        filters=described,
        summary=await summary(session, covered),
        categories=await by_category(session, covered),
        trend=await trend(session, covered, interval, zone),
        zone=zone,
        rows=await export_rows(session, covered, EXPORT_ROW_LIMIT),
        row_limit=EXPORT_ROW_LIMIT,
        trend_limit=MAX_TREND_POINTS,
    )
    content = await asyncio.to_thread(build)  # building the workbook is CPU-bound
    return content, f"analytics-{filters.source.value}-{utcnow():%Y%m%d-%H%M%S}.xlsx"
