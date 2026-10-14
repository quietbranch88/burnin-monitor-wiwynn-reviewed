from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.time import UTCDateTime
from app.schemas.common import ORMModel


class ReadingOut(ORMModel):
    server: str
    category: str
    sensor: str = Field(description="One of the server's sensors in the category, e.g. GPU3")
    value: float
    threshold: float
    is_anomaly: bool
    generated_at: UTCDateTime


class PersistedReadingOut(ReadingOut):
    id: int
    persisted_at: UTCDateTime


class HistoryPage(BaseModel):
    """Newest generation time first. For the next, older page pass `next_before_at` and `next_before_id`
    as `before_at` and `before_id`; both are null on the last page."""

    items: list[PersistedReadingOut]
    next_before_at: UTCDateTime | None
    next_before_id: int | None


class AlertEventOut(ORMModel):
    """One alert of one sensor, from its first reading past the limit to its first reading back inside."""

    id: int
    server: str
    category: str
    sensor: str
    started_at: UTCDateTime
    ended_at: UTCDateTime | None = Field(description="Null while the alert is open, or when the API stopped during it")
    threshold: float
    peak_value: float = Field(description="The reading furthest past the limit during the alert")
    readings: int = Field(description="Readings past the limit, one per second")
    acknowledged_at: UTCDateTime | None
    acknowledged_by: str | None = Field(description="Username of the first user to acknowledge the event")


class AlertEventAcknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: Literal[True]

    @field_validator("acknowledged", mode="before")
    @classmethod
    def only_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Only true is allowed")
        return value


class AlertEventPage(BaseModel):
    """Newest start first. For the next, older page pass `next_before_at` and `next_before_id`
    as `before_at` and `before_id`; both are null on the last page."""

    items: list[AlertEventOut]
    next_before_at: UTCDateTime | None
    next_before_id: int | None


class ServerReading(BaseModel):
    server: str
    category: str
    sensor: str
    value: float


class TickCategorySummary(BaseModel):
    count: int
    average: float
    minimum: float
    maximum: float
    threshold: float
    anomalies: int
    alert_below: bool = Field(description="True when a reading below the threshold is the alert")
    worst: float = Field(description="The reading nearest the limit: the maximum, or the minimum when alert_below")
    worst_server: str
    worst_sensor: str


class TickOut(BaseModel):
    """One generator tick across the fleet; the same shape is pushed over WebSocket."""

    type: Literal["tick"]
    generated_at: UTCDateTime
    servers: int
    readings: int
    warning_percent: int
    summary: dict[str, TickCategorySummary]
    alerts: list[ReadingOut]
    hottest: list[ReadingOut]
    server_readings: list[ServerReading] | None = Field(
        None,
        description="WebSocket only, for fleets of up to 200 servers: per server and category, the sensor nearest its limit.",
    )


class RealtimeConfig(BaseModel):
    interval_seconds: float
    thresholds: dict[str, float]
    sensors: dict[str, list[str]] = Field(description="Each server's sensors per category")
    alert_below: list[str] = Field(description="Categories whose alert is a reading below the threshold")
    warning_percent: int
    servers: int
    readings_per_second: float
    flush_interval_seconds: float
    flush_batch_size: int
    retention_hours: float
