import json
from pathlib import Path

from app.db.session import SessionLocal
from app.models import Record
from sqlalchemy import func, select

DATA = Path("/data") if Path("/data/sample_records.csv").exists() else Path(__file__).resolve().parents[2] / "data"
URL = "/api/v1/records/import"
CSV = "title,value,category,recorded_at,server\nA,1.5,cpu_temp,2026-10-01T00:00:00Z,S0001\nB,2,power_w,2026-10-02T00:00:00Z,S0001\n"


async def count_records() -> int:
    async with SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(Record)) or 0


async def test_csv_import_assigns_owner(client, accounts):
    files = {"file": ("records.csv", "﻿" + CSV, "text/csv")}  # with an Excel BOM
    response = await client.post(URL, files=files, headers=accounts.engineer.headers)
    assert response.status_code == 201 and response.json() == {"imported": 2}
    listed = (await client.get("/api/v1/records", params={"mine": True}, headers=accounts.engineer.headers)).json()
    assert listed["total"] == 2


async def test_json_import_accepts_list_or_object(client, accounts):
    rows = [{"title": "J", "value": 3, "category": "fan_rpm", "recorded_at": "2026-10-03T00:00:00Z", "server": "S0001"}]
    for payload in (rows, {"records": rows}):
        files = {"file": ("records.json", json.dumps(payload), "application/json")}
        assert (await client.post(URL, files=files, headers=accounts.engineer.headers)).status_code == 201
    assert await count_records() == 2


async def test_invalid_rows_reject_whole_file(client, accounts):
    content = (DATA / "sample_records_invalid.csv").read_bytes()
    response = await client.post(URL, files={"file": ("bad.csv", content, "text/csv")}, headers=accounts.engineer.headers)
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert {e["row"] for e in errors} == {3, 4, 5}
    messages = {e["field"]: e["message"] for e in errors}
    assert messages["title"] == "title must not be blank"
    assert "lower-case letters" in messages["category"] and "^[" not in messages["category"]
    assert await count_records() == 0


async def test_import_rejects_bad_files_and_viewers(client, accounts):
    headers = accounts.engineer.headers
    missing = await client.post(URL, files={"file": ("x.csv", "title,value\nA,1\n", "text/csv")}, headers=headers)
    assert missing.status_code == 422 and "missing columns" in missing.json()["detail"]
    wrong_type = await client.post(URL, files={"file": ("x.txt", "hello", "text/plain")}, headers=headers)
    assert wrong_type.status_code == 422
    broken = await client.post(URL, files={"file": ("x.json", "{oops", "application/json")}, headers=headers)
    assert broken.status_code == 422
    viewer = await client.post(URL, files={"file": ("x.csv", CSV, "text/csv")}, headers=accounts.viewer.headers)
    assert viewer.status_code == 403


async def test_bundled_samples_import(client, accounts):
    for name, media in (("sample_records.csv", "text/csv"), ("sample_records.json", "application/json")):
        content = (DATA / name).read_bytes()
        response = await client.post(URL, files={"file": (name, content, media)}, headers=accounts.engineer.headers)
        assert response.status_code == 201, response.text
    assert await count_records() == 112 + 10


async def test_oversized_upload_is_rejected_before_parsing(client, accounts):
    big = "x" * (3 * 1024 * 1024)
    response = await client.post(URL, files={"file": ("big.csv", big, "text/csv")}, headers=accounts.engineer.headers)
    assert response.status_code == 413 and response.headers["X-Request-ID"]
