"""Malformed imports and unrepresentable UTC instants are validation errors."""

import json
from datetime import UTC, datetime

import pytest
from app.core.errors import ValidationFailedError
from app.core.time import to_storage
from app.db.session import SessionLocal
from app.models import Record
from app.schemas.record import RecordCreate, RecordUpdate
from app.services.importer import parse_records
from pydantic import ValidationError
from sqlalchemy import func, select

BASE = {"title": "UTC boundary", "value": 1, "category": "cpu_temp",
        "recorded_at": "2026-10-01T08:00:00Z", "server": "S0001"}
EXTREME_DATES = ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00")
SENTINELS: tuple[object, ...] = (1, None, True, "extra", [], {})
QUERY_PATHS = ("/records", "/admin/logs", "/admin/realtime/history",
               "/analytics/summary", "/analytics/categories", "/analytics/trend", "/analytics/export")


@pytest.mark.parametrize("value", SENTINELS)
def test_parser_rejects_json_csv_sentinel_with_row_and_field(value):
    raw = json.dumps([BASE, BASE | {"__extra_cells__": value}]).encode()
    with pytest.raises(ValidationFailedError) as caught:
        parse_records(raw, "records.json", "application/json")
    assert caught.value.errors == [{"row": 2, "field": "__extra_cells__", "message": "Extra inputs are not permitted"}]


@pytest.mark.parametrize("value", EXTREME_DATES)
@pytest.mark.parametrize("schema", (RecordCreate, RecordUpdate))
def test_schema_rejects_unrepresentable_utc(value, schema):
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(BASE | {"recorded_at": value})
    assert caught.value.errors()[0]["loc"] == ("recorded_at",)


@pytest.mark.parametrize("value", EXTREME_DATES)
def test_storage_rejects_unrepresentable_utc(value):
    with pytest.raises(ValueError, match="UTC"):
        to_storage(datetime.fromisoformat(value))


@pytest.mark.parametrize("value", ("2026-10-01T16:00:00+08:00", "2026-10-01T08:00:00"))
def test_schema_preserves_timezone_and_naive_input_contract(value):
    parsed = RecordCreate.model_validate(BASE | {"recorded_at": value})
    assert parsed.recorded_at == datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert to_storage(parsed.recorded_at) == datetime(2026, 10, 1, 8)


async def record_count():
    async with SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(Record))


@pytest.mark.parametrize("value", SENTINELS)
async def test_api_json_csv_sentinel_rejects_whole_import(client, accounts, value):
    payload = json.dumps([BASE, BASE | {"__extra_cells__": value}])
    response = await client.post("/api/v1/records/import", headers=accounts.engineer.headers,
                                 files={"file": ("records.json", payload, "application/json")})
    assert response.status_code == 422, response.text
    assert response.json()["errors"] == [{"row": 2, "field": "__extra_cells__", "message": "Extra inputs are not permitted"}]
    assert await record_count() == 0


@pytest.mark.parametrize("value", EXTREME_DATES)
async def test_api_invalid_utc_create_update_and_import_leave_records_unchanged(client, accounts, value):
    headers = accounts.engineer.headers
    created = await client.post("/api/v1/records", headers=headers, json=BASE)
    assert created.status_code == 201
    invalid = BASE | {"recorded_at": value}
    for method, path in (("POST", "/api/v1/records"), ("PATCH", f"/api/v1/records/{created.json()['id']}")):
        response = await client.request(method, path, headers=headers, json=invalid)
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "recorded_at"
    for filename, media, payload, row in (
        ("records.json", "application/json", json.dumps([BASE, invalid]), 2),
        ("records.csv", "text/csv", "title,value,category,recorded_at,server\n"
         f"Valid,1,cpu_temp,2026-10-01T08:00:00Z,S0001\nInvalid,1,cpu_temp,{value},S0001\n", 3),
    ):
        response = await client.post("/api/v1/records/import", headers=headers,
                                     files={"file": (filename, payload, media)})
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["row"] == row
        assert response.json()["errors"][0]["field"] == "recorded_at"
    assert await record_count() == 1
    stored = await client.get(f"/api/v1/records/{created.json()['id']}", headers=headers)
    assert stored.json()["recorded_at"] == BASE["recorded_at"]


@pytest.mark.parametrize("value", EXTREME_DATES)
@pytest.mark.parametrize("field", ("start", "end"))
@pytest.mark.parametrize("path", QUERY_PATHS)
async def test_api_query_dates_reject_unrepresentable_utc(client, accounts, value, field, path):
    response = await client.get("/api/v1" + path, headers=accounts.admin.headers, params={field: value})
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == "query." + field


@pytest.mark.parametrize("value", ("2026-10-01T16:00:00+08:00", "2026-10-01T08:00:00"))
async def test_api_query_dates_preserve_inclusive_timezone_and_naive_contract(client, accounts, value):
    created = await client.post("/api/v1/records", headers=accounts.engineer.headers, json=BASE)
    assert created.status_code == 201
    response = await client.get("/api/v1/records", headers=accounts.engineer.headers,
                                params={"start": value, "end": value})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [created.json()["id"]]
