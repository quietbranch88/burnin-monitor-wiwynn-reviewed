"""Write log records to the system_logs table through a batch writer."""

import asyncio
import logging
from contextvars import ContextVar

from app.core.config import Settings
from app.core.time import utcnow
from app.db.session import SessionLocal, is_rejected_row
from app.models.system_log import SystemLog
from app.services.batch import BatchWriter

# Set by the request middleware, so event logs such as "role changed" carry the request id too.
current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)

# Field -> maximum length of the matching system_logs column (None for numbers).
REQUEST_FIELDS = {
    "request_id": 36,
    "method": 10,
    "path": 500,
    "status_code": None,
    "duration_ms": None,
    "user_id": None,
    "client_ip": 45,
}


async def write_logs(batch: list[dict]) -> None:
    async with SessionLocal() as session:
        session.add_all(SystemLog(**entry) for entry in batch)
        await session.commit()


def build_log_writer(settings: Settings) -> BatchWriter[dict]:
    return BatchWriter(
        "system_logs",
        write_logs,
        batch_size=settings.log_flush_batch_size,
        interval=settings.log_flush_interval_seconds,
        max_pending=10000,
        is_permanent=is_rejected_row,
    )


class DatabaseLogHandler(logging.Handler):
    """Queue `app.*` records at INFO and above; never blocks the caller."""

    def __init__(self, writer: BatchWriter[dict]) -> None:
        super().__init__(level=logging.INFO)
        self.writer = writer
        # Created in the app's lifespan, so this is the event loop the writer belongs to.
        self.loop = asyncio.get_running_loop()

    def filter(self, record: logging.LogRecord) -> bool:
        # The batch writer logs its own failures; storing them would loop.
        return record.name.startswith("app.") and record.name != "app.batch"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "created_at": utcnow(),
                "level": record.levelname,
                "logger": record.name[:100],
                "message": record.getMessage()[:4000],
            }
            for field, limit in REQUEST_FIELDS.items():
                value = getattr(record, field, None)
                entry[field] = value[:limit] if limit and isinstance(value, str) else value
            entry["request_id"] = entry["request_id"] or current_request_id.get()
            if self._in_loop_thread():
                self.writer.add(entry)
            else:
                # From a worker thread (asyncio.to_thread): the writer and its event are not
                # thread-safe, so hand the entry to the loop.
                self.loop.call_soon_threadsafe(self.writer.add, entry)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


    def _in_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False


class RequestIdFilter(logging.Filter):
    """Put the current request id on every record, so a console line can be matched to the
    `X-Request-ID` a client reports even while the database (and its log table) is down."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = current_request_id.get() or "-"
        return True


def configure_logging(level: str) -> None:
    """Console output follows LOG_LEVEL; `app.*` always emits INFO so the database
    handler keeps request logs and events even when the console is quieter."""
    root = logging.getLogger()
    if not any(getattr(h, "_monitor_console", False) for h in root.handlers):
        console = logging.StreamHandler()
        console.addFilter(RequestIdFilter())
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
        console._monitor_console = True  # type: ignore[attr-defined]  # a marker, so a second call finds this handler
        root.addHandler(console)
    for handler in root.handlers:
        if getattr(handler, "_monitor_console", False):
            handler.setLevel(level.upper())
    root.setLevel(level.upper())
    logging.getLogger("app").setLevel(logging.INFO)
