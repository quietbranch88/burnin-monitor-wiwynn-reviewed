from datetime import timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import ValidationFailedError
from app.core.time import TimeZoneName, UTCDateTime, to_storage, utcnow
from app.schemas.analytics import CategorySummary, Interval, Source, Summary, TrendPoint
from app.services import analytics
from app.services.analytics import AnalyticsFilters

router = APIRouter(prefix="/analytics", tags=["analytics"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RANGE_DOC = ("Inclusive, ISO 8601. Live telemetry covers the whole minutes inside the range, "
             "up to the last finished minute, for the fleet and for one server alike.")
# One server's live-telemetry figures are aggregated from its raw readings (the statistics cover
# the fleet), and any signed-in user may ask: the range is bounded so one request cannot scan
# the hours of raw rows that retention keeps.
ONE_SERVER_MAX_HOURS = 3
ONE_SERVER_DEFAULT_HOURS = 1


def get_filters(
    source: Source = Source.records,
    start: Annotated[UTCDateTime | None, Query(description=RANGE_DOC)] = None,
    end: Annotated[UTCDateTime | None, Query(description=RANGE_DOC)] = None,
    category: Annotated[str | None, Query(max_length=50)] = None,
    server: Annotated[str | None, Query(max_length=20, description=(
        "One server, e.g. S0017. For live telemetry its figures come from its raw readings, so the range "
        f"spans at most {ONE_SERVER_MAX_HOURS} hours, the last {ONE_SERVER_DEFAULT_HOURS} hour without a start."))] = None,
) -> AnalyticsFilters:
    if start is not None and end is not None and to_storage(start) > to_storage(end):
        raise ValidationFailedError("start must not be after end", errors=[{"field": "query.end", "message": "Before start"}])
    if source == Source.realtime and server:
        until = to_storage(end) if end is not None else utcnow()
        if start is None:
            try:
                start = until - timedelta(hours=ONE_SERVER_DEFAULT_HOURS)
            except OverflowError as exc:
                raise ValidationFailedError(
                    "end is too early for the default live-telemetry range",
                    errors=[{"field": "query.end", "message": "Pass a later end or an explicit start"}],
                ) from exc
        elif until - to_storage(start) > timedelta(hours=ONE_SERVER_MAX_HOURS):
            raise ValidationFailedError(
                f"For one server the live-telemetry range may span at most {ONE_SERVER_MAX_HOURS} hours",
                errors=[{"field": "query.start", "message": f"At most {ONE_SERVER_MAX_HOURS} hours before end"}],
            )
    return AnalyticsFilters(source=source, start=start, end=end, category=category, server=server)


FiltersDep = Annotated[AnalyticsFilters, Depends(get_filters)]


def get_zone(
    tz: Annotated[TimeZoneName, Query(description="IANA time zone for trend buckets, e.g. Asia/Taipei")] = "UTC",
) -> ZoneInfo:
    return ZoneInfo(tz)  # the name was checked against the zone database by TimeZoneName


ZoneDep = Annotated[ZoneInfo, Depends(get_zone)]


@router.get("/summary", response_model=Summary)
async def summary(session: SessionDep, _: CurrentUser, filters: FiltersDep) -> Summary:
    """Count, total, average, minimum and maximum of `value`. Without `category` the figures span
    metrics with different units (°C, RPM, W): use `/analytics/categories` for meaningful ones."""
    return await analytics.summary(session, filters)


@router.get("/categories", response_model=list[CategorySummary])
async def by_category(session: SessionDep, _: CurrentUser, filters: FiltersDep) -> list[CategorySummary]:
    return await analytics.by_category(session, filters)


@router.get("/trend", response_model=list[TrendPoint])
async def trend(
    session: SessionDep,
    _: CurrentUser,
    filters: FiltersDep,
    zone: ZoneDep,
    interval: Interval = Interval.hour,
) -> list[TrendPoint]:
    """Per-category aggregates in time buckets; a day bucket runs from midnight in `tz`.
    At most the newest 5,000 buckets: choose a longer interval for a long range."""
    return await analytics.trend(session, filters, interval, zone)


@router.get("/export", response_class=Response, responses={200: {"content": {XLSX: {}}}})
async def export_excel(
    session: SessionDep, _: CurrentUser, filters: FiltersDep, zone: ZoneDep, interval: Interval = Interval.hour
) -> Response:
    """Excel workbook with Summary, By category, Trend and Data sheets. Trend buckets are in `tz`."""
    content, filename = await analytics.export_workbook(session, filters, interval, zone)
    return Response(content, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
