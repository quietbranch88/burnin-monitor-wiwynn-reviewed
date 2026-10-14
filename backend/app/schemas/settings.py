from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.time import TimeZoneName, UTCDateTime
from app.schemas.record import MAX_ABS_VALUE

Threshold = Annotated[float, Field(gt=0, le=MAX_ABS_VALUE, allow_inf_nan=False, strict=True)]


class ThresholdOut(BaseModel):
    category: str
    threshold: float
    default: float = Field(description="The value from .env")
    alert_below: bool = Field(description="True when a reading below the threshold is the alert, as for fan speed")


class SettingsOut(BaseModel):
    warning_percent: int = Field(description="A reading this close to its limit, in percent, is a warning")
    language: Literal["zh", "en"]
    time_zone: str = Field(description="The plant's IANA time zone; every station shows times in it")
    thresholds: list[ThresholdOut]
    updated_at: UTCDateTime | None
    updated_by: str | None


class SettingsUpdate(BaseModel):
    """Fields left out stay as they are."""

    model_config = ConfigDict(extra="forbid")

    warning_percent: Annotated[int, Field(ge=50, le=99, strict=True)] | None = None
    language: Literal["zh", "en"] | None = None
    time_zone: TimeZoneName | None = Field(None, description="IANA time zone of the plant, e.g. America/Ciudad_Juarez")
    thresholds: dict[str, Threshold] | None = Field(None, description="Metric to new threshold, e.g. {\"gpu_temp\": 83}")


class DisplaySettings(BaseModel):
    language: Literal["zh", "en"]
    time_zone: str
