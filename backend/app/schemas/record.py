import math
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.time import UTCDateTime
from app.schemas.common import ORMModel

CATEGORY_PATTERN = re.compile(r"[a-z0-9_-]{1,50}")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
SERVER_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,20}")
SERVER_RULE = "server must be 1-20 letters, digits, '.', '_' or '-'"
CATEGORY_RULE = "category must be 1-50 lower-case letters, digits, '_' or '-'"
FIRST_YEAR, LAST_YEAR = 1970, 2999
# Far beyond any sensor reading; keeps sums of many records inside DOUBLE's range.
MAX_ABS_VALUE = 1e15


class _RecordFields(BaseModel):
    """Validators shared by create and update; None means "not supplied" on update."""

    # A misspelt field such as "titel" must not answer 200 and change nothing.
    model_config = ConfigDict(extra="forbid")

    @field_validator("title", check_fields=False)
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        if CONTROL_CHARACTERS.search(value):
            # Excel cannot store them; one such title would break the report for everyone.
            raise ValueError("title must not contain control characters")
        return value

    @field_validator("category", mode="before", check_fields=False)
    @classmethod
    def normalise_category(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("category", check_fields=False)
    @classmethod
    def check_category(cls, value: str | None) -> str | None:
        if value is not None and not CATEGORY_PATTERN.fullmatch(value):
            raise ValueError(CATEGORY_RULE)
        return value

    @field_validator("server", mode="before", check_fields=False)
    @classmethod
    def strip_server(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("server", check_fields=False)
    @classmethod
    def check_server(cls, value: str | None) -> str | None:
        if value is not None and not SERVER_PATTERN.fullmatch(value):
            raise ValueError(SERVER_RULE)
        return value

    @field_validator("recorded_at", check_fields=False)
    @classmethod
    def plausible_time(cls, value: datetime | None) -> datetime | None:
        # Keeps time zone shifts in analytics inside the range MariaDB and Python can represent.
        if value is not None and not FIRST_YEAR <= value.year <= LAST_YEAR:
            raise ValueError(f"recorded_at must be between {FIRST_YEAR} and {LAST_YEAR}")
        return value

    @field_validator("value", mode="before", check_fields=False)
    @classmethod
    def not_a_boolean(cls, value: object) -> object:
        if isinstance(value, bool):  # JSON true would otherwise be stored as 1.0
            raise ValueError("value must be a number, not true or false")
        return value

    @field_validator("value", check_fields=False)
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("value must be a finite number")
        if value is not None and abs(value) > MAX_ABS_VALUE:
            raise ValueError(f"value must be between -{MAX_ABS_VALUE:.0e} and {MAX_ABS_VALUE:.0e}")
        return value


class RecordCreate(_RecordFields):
    title: str = Field(max_length=200)
    value: float
    category: str = Field(description="1-50 lower-case letters, digits, '_' or '-'")
    recorded_at: UTCDateTime = Field(description="ISO 8601; a value without offset is taken as UTC")
    server: str = Field(description="The server under test, as in the live telemetry, e.g. S0017")


class RecordUpdate(_RecordFields):
    """Partial update: only supplied fields change."""

    title: str | None = Field(None, max_length=200)
    value: float | None = None
    category: str | None = None
    recorded_at: UTCDateTime | None = None
    server: str | None = None

    @field_validator("title", "value", "category", "recorded_at", "server", mode="before")
    @classmethod
    def not_null(cls, value: object) -> object:
        # Leave a field out to keep it; an explicit null would silently change nothing.
        if value is None:
            raise ValueError("must not be null; leave the field out to keep it")
        return value


class RecordOut(ORMModel):
    id: int
    title: str
    value: float
    category: str
    server: str
    recorded_at: UTCDateTime
    owner_id: int
    owner_username: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class RecordSort(StrEnum):
    recorded_at = "recorded_at"
    value = "value"
    title = "title"  # type: ignore[assignment]  # an enum member shadows str.title, as intended
    category = "category"
    created_at = "created_at"
    id = "id"


class SortOrder(StrEnum):
    asc = "asc"
    desc = "desc"


class ImportResult(BaseModel):
    imported: int
