"""UTC helpers. The database stores naive UTC DATETIME(6); the API speaks aware UTC."""

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from zoneinfo import available_timezones

from pydantic import AfterValidator, Field


def utcnow() -> datetime:
    """Current time as naive UTC, the storage representation."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_storage(value: datetime) -> datetime:
    """Convert any datetime to naive UTC. Naive input is taken to be UTC already."""
    return _as_aware_utc(value).replace(tzinfo=None)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        # ValueError is a normal Pydantic validation error for body and query inputs.
        raise ValueError("datetime is outside the supported UTC range") from exc


# Use for every datetime field in API schemas: output always carries "Z".
UTCDateTime = Annotated[datetime, AfterValidator(_as_aware_utc)]


@lru_cache(maxsize=1)
def _zone_names() -> frozenset[str]:
    # The zone directory also holds files that are not zone names: the host's own time and a template.
    return frozenset(available_timezones()) - {"localtime", "posixrules"}


def _known_zone(name: str) -> str:
    # Membership, not ZoneInfo(name), which would also open a relative path inside the zone directory.
    if name not in _zone_names():
        raise ValueError(f"Unknown time zone: {name}")
    return name


# An IANA time zone name such as Asia/Taipei.
TimeZoneName = Annotated[str, Field(max_length=64), AfterValidator(_known_zone)]
