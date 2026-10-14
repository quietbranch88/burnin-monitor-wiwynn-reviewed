from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.core.time import UTCDateTime
from app.models.user import Role
from app.schemas.auth import PasswordRules, RegisterRequest
from app.schemas.common import ORMModel


class UserAdminCreate(RegisterRequest):
    """An account an admin creates, with any role (self-registration always gives Viewer)."""

    role: Role = Role.viewer


class PasswordReset(PasswordRules):
    """A new password chosen by an admin; it ends every session of that user."""


class UserAdminUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: StrictBool | None = None  # "no" or 0 must not pass as false


class SystemLogOut(ORMModel):
    id: int
    created_at: UTCDateTime
    level: str
    logger: str
    message: str
    request_id: str | None
    method: str | None
    path: str | None
    status_code: int | None
    duration_ms: float | None
    user_id: int | None
    username: str | None = None
    client_ip: str | None


class PoolStatus(BaseModel):
    size: int
    checked_in: int
    checked_out: int
    overflow: int
    configured_size: int
    configured_max_overflow: int


class WriterStatus(BaseModel):
    pending: int = Field(description="All retained items: queued plus in-flight")
    queued: int = 0
    in_flight: int = 0
    written: int
    dropped: int
    flushes: int
    last_flush_at: UTCDateTime | None
    last_error: str | None
    backoff_seconds: float  # > 0 while the writer waits between retries after a failed write


class DatabaseStatus(BaseModel):
    dialect: str
    server_version: str
    ping_ms: float
    pool: PoolStatus
    table_rows: dict[str, int]
    approximate_rows: list[str]
    realtime_writer: WriterStatus
    minute_writer: WriterStatus
    alert_writer: WriterStatus
    log_writer: WriterStatus
    skipped_ticks: int = Field(description="Seconds without readings because the API was held up; should stay 0")
    websocket_clients: int
    generator_running: bool = Field(description="This process holds the generator lease and simulates the fleet")
