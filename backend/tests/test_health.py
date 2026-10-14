"""The health check, and what every client sees of an error: one shape, a request id, no internal detail."""

import httpx
from app.api import health
from app.services import records


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok", "database": "ok"}
    assert (await client.get("/health/live")).json() == {"status": "ok"}


async def test_errors_carry_request_id(client):
    response = await client.get("/api/v1/records", headers={"X-Request-ID": "trace-123"})
    assert response.status_code == 401
    assert response.json()["request_id"] == "trace-123" and response.headers["X-Request-ID"] == "trace-123"


async def test_large_json_is_gzipped_but_small_responses_and_excel_are_not(client, accounts):
    gzip = {"Accept-Encoding": "gzip"}
    large = await client.get("/openapi.json", headers=gzip)
    assert large.headers.get("content-encoding") == "gzip" and large.json()["openapi"]
    small = await client.get("/health", headers=gzip)
    assert "content-encoding" not in small.headers
    excel = await client.get("/api/v1/analytics/export", headers=gzip | accounts.viewer.headers)
    assert excel.status_code == 200 and "content-encoding" not in excel.headers


async def test_database_outage_answers_503_with_retry_after(client, accounts, monkeypatch):
    from asyncmy.errors import OperationalError as DriverError
    from sqlalchemy.exc import OperationalError

    async def unreachable(_session):
        raise OperationalError("SELECT 1", {}, DriverError(2003, "Can't connect to MySQL server on 'db'"))

    monkeypatch.setattr(records, "list_categories", unreachable)
    response = await client.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert response.json()["detail"] == "Database unavailable, please retry shortly"
    assert "MySQL" not in response.text and response.headers["X-Request-ID"] == response.json()["request_id"]


async def test_unhandled_error_is_safe_and_traceable(app, accounts, monkeypatch):
    async def boom(_session):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(records, "list_categories", boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error" and "secret" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


async def test_health_reports_an_unreachable_database(client, monkeypatch):
    class Unreachable:
        async def __aenter__(self):
            raise ConnectionRefusedError("database down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(health, "SessionLocal", Unreachable)
    response = await client.get("/health")
    assert response.status_code == 503 and response.json() == {"status": "degraded", "database": "unreachable"}
    assert (await client.get("/health/live")).status_code == 200  # the process is fine; the container stays up
