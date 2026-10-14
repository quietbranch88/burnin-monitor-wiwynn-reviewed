"""Application settings, read from environment variables (see .env.example)."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.time import TimeZoneName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "Burn-in Monitor API"
    log_level: Annotated[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], BeforeValidator(str.upper)] = "INFO"

    # Database: async SQLAlchemy engine over asyncmy.
    database_url: str = "mariadb+asyncmy://monitor:monitor@db:3306/monitor?charset=utf8mb4"
    db_pool_size: int = Field(10, ge=1, le=100)
    db_max_overflow: int = Field(20, ge=0, le=100)
    # Seconds to wait for a free connection before answering 503. Below the Streamlit
    # client's 15 s timeout, so the caller gets the retry advice instead of giving up first.
    db_pool_timeout: int = Field(10, ge=1)
    # Below MariaDB's default wait_timeout (28800 s) so idle connections are
    # recycled before the server drops them.
    db_pool_recycle: int = Field(1800, ge=60)
    db_echo: bool = False

    # Authentication.
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    # One 8-hour shift: a station showing the live page stays signed in until the shift ends.
    jwt_expire_minutes: int = Field(480, ge=1, le=1440)
    bcrypt_rounds: int = Field(12, ge=4, le=15)

    # Real-time generator and batch writer.
    realtime_enabled: bool = True
    realtime_interval_seconds: float = Field(1.0, gt=0)
    # "category:threshold" pairs; the categories are generated in this order.
    # Each server has several sensors per category (app/services/realtime.py SENSORS); a
    # category without a sensor list there reports one sensor.
    realtime_thresholds: Annotated[dict[str, float], NoDecode] = {
        "chamber_temp": 45.0,
        "cpu_temp": 90.0,
        "gpu_temp": 85.0,
        "dimm_temp": 85.0,
        "nvme_temp": 70.0,
        "fan_rpm": 3000.0,
        "power_w": 2700.0,
    }
    # Metrics whose alert is a reading below the threshold: a failing fan slows down.
    realtime_alert_below: Annotated[set[str], NoDecode] = {"fan_rpm"}
    # First values of the settings an admin changes at runtime (table system_settings).
    realtime_warning_percent: int = Field(90, ge=50, le=99)
    display_language: Literal["zh", "en"] = "en"
    # The plant's time zone: every station shows and enters times in it.
    display_tz: TimeZoneName = "Asia/Taipei"
    # Servers under burn-in; each reports every sensor once per interval (50 with the
    # default categories, so 2,000 servers are 100,000 readings/s).
    realtime_servers: int = Field(20, ge=1, le=2000)
    realtime_flush_interval_seconds: float = Field(10.0, gt=0)
    realtime_flush_batch_size: int = Field(1000, ge=1)
    # Per-tick summaries kept in memory for dashboards that connect late (600 = 10 min at 1 tick/s).
    realtime_recent_size: int = Field(600, ge=10)
    realtime_max_pending: int = Field(500_000, ge=100)
    # Chunks of one flush written in parallel, each on its own pooled connection.
    realtime_write_concurrency: int = Field(4, ge=1, le=8)
    # Raw readings older than this are purged; per-minute statistics are kept.
    realtime_retention_hours: float = Field(12.0, gt=0)
    log_retention_days: float = Field(14.0, gt=0)
    # Purges readings, logs and idempotency keys; independent of the generator.
    retention_enabled: bool = True

    # System log batch writer.
    log_flush_interval_seconds: float = Field(5.0, gt=0)
    log_flush_batch_size: int = Field(100, ge=1)

    # Demo accounts created by `python -m app.seed`.
    seed_admin_password: str = "Admin@12345"
    seed_user_password: str = "User@12345"
    seed_viewer_password: str = "Viewer@12345"
    seed_sample_csv: str = "/data/sample_records.csv"

    @field_validator("realtime_alert_below", mode="before")
    @classmethod
    def parse_alert_below(cls, value: object) -> object:
        if isinstance(value, str):
            return {name.strip() for name in value.split(",") if name.strip()}
        return value

    @field_validator("realtime_thresholds", mode="before")
    @classmethod
    def parse_thresholds(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        pairs = {}
        for item in value.split(","):
            name, sep, threshold = item.strip().partition(":")
            if not sep or not name:
                raise ValueError("REALTIME_THRESHOLDS must look like 'cpu_temp:90,power_w:2700'")
            pairs[name.strip()] = float(threshold)
        return pairs

    @field_validator("realtime_thresholds")
    @classmethod
    def check_thresholds(cls, value: dict[str, float]) -> dict[str, float]:
        # Readings are compared as value / threshold, and categories are stored in 50 characters.
        bad = [name for name, threshold in value.items() if not 0 < threshold < float("inf") or len(name) > 50]
        if not value or bad:
            raise ValueError(f"REALTIME_THRESHOLDS needs positive thresholds and names up to 50 characters: {bad}")
        return value

    @model_validator(mode="after")
    def check_alert_below(self) -> "Settings":
        # A misspelt name would otherwise be ignored, and fans would alert when fast instead of slow.
        # Only a value that was set is checked: the default may name a metric a custom list leaves out.
        unknown = sorted(self.realtime_alert_below - set(self.realtime_thresholds))
        if unknown and "realtime_alert_below" in self.model_fields_set:
            raise ValueError(f"REALTIME_ALERT_BELOW names metrics missing from REALTIME_THRESHOLDS: {unknown}")
        return self


def load_settings() -> Settings:
    """Read the settings once; a bad value stops the start with its name, never its value."""
    try:
        # pydantic-settings fills the fields from the environment; mypy sees required arguments.
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Names and reasons only: pydantic's own message prints the values, secrets included.
        lines = [
            f"  {'.'.join(str(part) for part in error['loc']).upper() or 'settings'}: "
            f"{error['msg'].removeprefix('Value error, ')}"
            for error in exc.errors(include_input=False)
        ]
        raise SystemExit("Invalid settings in the environment or .env:\n" + "\n".join(lines)) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
