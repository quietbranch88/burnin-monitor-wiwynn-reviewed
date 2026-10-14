import asyncio

from app.db.session import SessionLocal
from app.models import IdempotencyKey, Record
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

BODY = {"title": "Line 2 station 07 - fan speed", "value": 12000.0, "category": "fan_rpm",
        "recorded_at": "2026-10-01T08:00:00Z", "server": "S0019"}
CSV = b"title,value,category,recorded_at,server\nA,1,cpu_temp,2026-10-01T08:00:00Z,S0001\nB,2,cpu_temp,2026-10-01T09:00:00Z,S0001\n"


async def count_records() -> int:
    async with SessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(Record))).scalar_one()


async def test_a_retried_create_returns_the_stored_record_and_creates_nothing(client, accounts):
    headers = accounts.engineer.headers | {"Idempotency-Key": "create-1"}
    first = await client.post("/api/v1/records", json=BODY, headers=headers)
    retry = await client.post("/api/v1/records", json=BODY, headers=headers)  # the first response was lost
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json() and retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await count_records() == 1


async def test_a_key_reused_for_a_different_request_is_refused(client, accounts):
    headers = accounts.engineer.headers | {"Idempotency-Key": "create-2"}
    assert (await client.post("/api/v1/records", json=BODY, headers=headers)).status_code == 201
    other = await client.post("/api/v1/records", json=BODY | {"value": 1.0}, headers=headers)
    assert other.status_code == 409 and "different request" in other.json()["detail"]
    assert await count_records() == 1


async def test_without_a_key_every_request_creates(client, accounts):
    for _ in range(2):
        assert (await client.post("/api/v1/records", json=BODY, headers=accounts.engineer.headers)).status_code == 201
    assert await count_records() == 2


async def test_keys_belong_to_one_user(client, accounts):
    for account in (accounts.engineer, accounts.other):
        response = await client.post("/api/v1/records", json=BODY, headers=account.headers | {"Idempotency-Key": "same"})
        assert response.status_code == 201 and response.json()["owner_username"] == account.user.username
    assert await count_records() == 2


async def test_a_retried_import_is_stored_once(client, accounts):
    headers = accounts.engineer.headers | {"Idempotency-Key": "import-1"}
    files = {"file": ("rows.csv", CSV, "text/csv")}
    first = await client.post("/api/v1/records/import", files=files, headers=headers)
    retry = await client.post("/api/v1/records/import", files=files, headers=headers)
    assert first.json() == retry.json() == {"imported": 2} and retry.headers["Idempotent-Replayed"] == "true"
    assert await count_records() == 2
    other = {"file": ("rows.csv", CSV + b"C,3,cpu_temp,2026-10-01T10:00:00Z,S0001\n", "text/csv")}
    changed = await client.post("/api/v1/records/import", files=other, headers=headers)
    assert changed.status_code == 409


async def test_a_failed_request_stores_no_key(client, accounts):
    headers = accounts.engineer.headers | {"Idempotency-Key": "bad-1"}
    bad = b"title,value,category,recorded_at,server\nA,not-a-number,cpu_temp,2026-10-01T08:00:00Z,S0001\n"
    assert (await client.post("/api/v1/records/import", files={"file": ("bad.csv", bad, "text/csv")}, headers=headers)).status_code == 422
    async with SessionLocal() as session:
        assert await session.get(IdempotencyKey, (accounts.engineer.user.id, "bad-1")) is None


async def test_a_request_that_loses_the_race_on_commit_gets_the_stored_result(client, accounts, monkeypatch):
    """Through the HTTP route: the lookup misses, the commit hits the stored key (1062)."""
    headers = accounts.engineer.headers | {"Idempotency-Key": "race"}
    first = await client.post("/api/v1/records", json=BODY, headers=headers)
    real_get, missed = AsyncSession.get, []

    async def lookup_misses_once(self, entity, ident, **kwargs):
        if entity is IdempotencyKey and not missed:
            missed.append(ident)
            return None  # as if the other request had not committed when this one looked
        return await real_get(self, entity, ident, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", lookup_misses_once)
    second = await client.post("/api/v1/records", json=BODY, headers=headers)
    assert missed and second.status_code == 201, second.text
    assert second.json() == first.json() and second.headers["Idempotent-Replayed"] == "true"
    assert await count_records() == 1


async def test_one_key_sent_five_times_at_once_creates_one_record(client, accounts):
    headers = accounts.engineer.headers | {"Idempotency-Key": "burst"}
    responses = await asyncio.gather(*(client.post("/api/v1/records", json=BODY, headers=headers) for _ in range(5)))
    assert [r.status_code for r in responses] == [201] * 5, [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses) == 4
    assert await count_records() == 1
