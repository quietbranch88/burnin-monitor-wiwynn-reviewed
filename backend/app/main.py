"""FastAPI application: routers, middleware, error handlers and background tasks."""

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import health
from app.api.analytics import XLSX as XLSX_MEDIA_TYPE
from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import DATABASE_ERRORS, engine, error_code, is_conflict, is_unavailable
from app.schemas.common import ErrorResponse
from app.services.importer import MAX_BYTES as IMPORT_MAX_BYTES
from app.services.importer import TOO_LARGE as IMPORT_TOO_LARGE
from app.services.realtime import RealtimeHub
from app.services.retention import RetentionJob
from app.services.system_logs import DatabaseLogHandler, build_log_writer, configure_logging, current_request_id

request_logger = logging.getLogger("app.request")
logger = logging.getLogger("app.main")

# High-frequency or tooling endpoints that would flood the system log table.
UNLOGGED_PATHS = {"/health", "/health/live", "/docs", "/redoc", "/openapi.json", "/api/v1/realtime/latest"}
# Polled by the frontend (role check every 15 s): logged only when it fails, so the system
# log shows events instead of thousands of identical lines.
QUIET_PATHS = {"/api/v1/auth/me"}
IMPORT_PATH = "/api/v1/records/import"
RETRY_AFTER_SECONDS = 5
SETTINGS_READ_TIMEOUT_SECONDS = 5
# Multipart framing around the file; the importer enforces the exact file limit.
IMPORT_MAX_REQUEST_BYTES = IMPORT_MAX_BYTES + 64 * 1024


class LimitImportBody:
    """Count an import request's body as it arrives and answer 413 past the limit.

    The request middleware already rejects a too-large Content-Length up front; this also
    covers a chunked upload that sends no Content-Length. Raising inside the body stream
    would not work: FastAPI turns any error while parsing a form into a 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != IMPORT_PATH:
            await self.app(scope, receive, send)
            return
        received, too_large = 0, False

        async def counted() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            received += len(message.get("body", b""))
            if received > IMPORT_MAX_REQUEST_BYTES:
                too_large = True
                return {"type": "http.disconnect"}  # the app stops reading the upload
            return message

        async def guarded(message: Message) -> None:
            if not too_large:
                await send(message)  # past the limit the app's own answer is replaced below

        await self.app(scope, counted, guarded)
        if too_large:
            request_id = scope.get("state", {}).get("request_id")
            response = JSONResponse({"detail": IMPORT_TOO_LARGE, "request_id": request_id}, status_code=413)
            await response(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log_writer = build_log_writer(settings)
    handler = DatabaseLogHandler(log_writer)
    logging.getLogger("app").addHandler(handler)
    log_writer.start()

    hub = RealtimeHub(settings)
    app.state.hub = hub
    app.state.log_writer = log_writer
    # The purge pauses while the writer is behind: more than one batch plus 3 seconds of readings waiting.
    behind = settings.realtime_flush_batch_size + 3 * hub.readings_per_second
    retention = RetentionJob(settings, busy=lambda: hub.writer.pending > behind)
    if settings.realtime_enabled:
        # Only the lease holder schedules recovery; it runs in the background over
        # completed minutes, outside the new generator's active minute.
        try:
            # The thresholds an admin saved decide what the first tick stores as an alert; the
            # .env values are only the fallback while the database does not answer.
            await asyncio.wait_for(hub.sync_settings(), timeout=SETTINGS_READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - same: the periodic sync applies them as soon as the database answers
            logger.warning("Could not read the alert settings before the first tick; using the .env thresholds until "
                           "the database answers", exc_info=True)
        hub.start()
    if settings.retention_enabled:
        retention.start()
    if settings.jwt_secret.startswith("change-me"):
        logger.warning("JWT_SECRET is still the .env.example placeholder: anyone who has read that file can forge tokens. "
                       "Acceptable only while the ports stay on 127.0.0.1 (the .env.example default); set a random "
                       "value before exposing the API")
    logger.info("API started")
    try:
        yield
    finally:
        await retention.stop()
        await hub.stop()
        logger.info("API stopping")
        await log_writer.stop()
        logging.getLogger("app").removeHandler(handler)
        await engine.dispose()


def _error(status_code: int, detail: object, request: Request, errors: list | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict = {"detail": detail, "request_id": request_id}
    if errors:
        body["errors"] = errors
    # Set here too: unhandled errors bypass the middleware that adds it to normal responses.
    headers = {"X-Request-ID": request_id} if request_id else {}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(body, status_code=status_code, headers=headers)


# A caller's X-Request-ID is kept only if it looks like an id; anything else gets a new one.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,36}")


def _document_error_shape(app: FastAPI) -> None:
    """OpenAPI shows FastAPI's default 422 body; ours is ErrorResponse. Replace it in the schema."""
    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = app.openapi_schema
        if schema is None:
            schema = default_openapi()  # also caches it in app.openapi_schema
            error = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components.update(error.pop("$defs", {}))
            components["HTTPValidationError"] = error
            components.pop("ValidationError", None)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # the way FastAPI documents customising the schema


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Burn-in test monitoring: records CRUD and import, real-time telemetry over WebSocket, "
            "analytics with Excel export, and administration. Authenticate with **Authorize** "
            "using a demo account from the README.\n\n"
            "**Live telemetry WebSocket** (OpenAPI cannot list it): `/api/v1/ws/realtime` with "
            "`Authorization: Bearer <JWT>` (browsers, which cannot set the header, may pass `?token=<JWT>`). "
            "One JSON message per second with the fleet summary, alerts and the hottest readings. "
            "A bad token is refused at the handshake; close code 1013 means the database is down, try again; "
            "1008 means the token expired or the account was disabled or logged out.\n\n"
            "Every error has the body `{detail, request_id, errors?}`."
        ),
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(api_router)
    _document_error_shape(app)

    # JSON lists compress about 13x (the 300 s tick summary: 725 KB to 55 KB). The Excel
    # workbook is already a zip file, so compressing it again would only cost CPU.
    app.add_middleware(LimitImportBody)
    app.add_middleware(
        GZipMiddleware,
        minimum_size=1000,
        compresslevel=6,
        exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, XLSX_MEDIA_TYPE),
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        given = request.headers.get("X-Request-ID", "")
        request.state.request_id = given if REQUEST_ID_PATTERN.fullmatch(given) else str(uuid.uuid4())
        current_request_id.set(request.state.request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            # Reject oversized uploads before the multipart parser spools them to disk.
            length = request.headers.get("content-length", "")
            if request.url.path == IMPORT_PATH and length.isdigit() and int(length) > IMPORT_MAX_REQUEST_BYTES:
                status_code = 413
                return _error(413, IMPORT_TOO_LARGE, request)
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            path = request.url.path
            if path not in UNLOGGED_PATHS and not (path in QUIET_PATHS and status_code < 400):
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                level = logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO
                request_logger.log(
                    level,
                    "%s %s -> %d (%.1f ms)",
                    request.method,
                    path,
                    status_code,
                    duration_ms,
                    extra={
                        "request_id": request.state.request_id,
                        "method": request.method,
                        "path": path[:500],
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "user_id": getattr(request.state, "user_id", None),
                        "client_ip": request.client.host if request.client else None,
                    },
                )

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        response = _error(exc.status_code, exc.detail, request, exc.errors)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(DataError)
    async def data_error(request: Request, exc: DataError) -> JSONResponse:
        # The database refused a value the schema let through, e.g. out of range for its column.
        logger.warning("Rejected value on %s %s: DataError %s", request.method, request.url.path, error_code(exc))
        return _error(422, "A value is out of range for its field", request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, exc.detail, request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                # A body that is not valid JSON has a character position as its location.
                "field": "body" if e["type"] == "json_invalid"
                else ".".join(str(p) for p in e["loc"] if p != "body") or "body",
                "message": e["msg"].removeprefix("Value error, "),
            }
            for e in exc.errors()
        ]
        return _error(422, "Request validation failed", request, errors)

    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _error(500, "Internal server error", request)

    async def database_error(request: Request, exc: Exception) -> JSONResponse:
        if is_unavailable(exc):
            # Expected during a database restart or outage: no traceback, and tell clients to retry.
            status_code, detail, retry_after = 503, "Database unavailable, please retry shortly", RETRY_AFTER_SECONDS
        elif is_conflict(exc):
            status_code, detail, retry_after = 409, "The data changed while saving, please retry", 1
        else:
            return await unexpected_error(request, exc)  # a bug, not an outage
        logger.warning("%s on %s %s: %s %s", detail, request.method, request.url.path, type(exc).__name__, error_code(exc))
        response = _error(status_code, detail, request)
        response.headers["Retry-After"] = str(retry_after)
        return response

    for error_class in DATABASE_ERRORS:
        app.add_exception_handler(error_class, database_error)
    app.add_exception_handler(Exception, unexpected_error)

    return app


app = create_app()
