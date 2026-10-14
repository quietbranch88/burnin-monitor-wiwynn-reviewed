from enum import StrEnum

from pydantic import BaseModel

from app.core.time import UTCDateTime


class Source(StrEnum):
    records = "records"
    realtime = "realtime"


class Interval(StrEnum):
    minute = "minute"
    hour = "hour"
    day = "day"


class Summary(BaseModel):
    count: int
    total: float | None
    average: float | None
    minimum: float | None
    maximum: float | None


class CategorySummary(Summary):
    category: str


class TrendPoint(BaseModel):
    bucket: UTCDateTime
    category: str
    count: int
    average: float
    minimum: float
    maximum: float
