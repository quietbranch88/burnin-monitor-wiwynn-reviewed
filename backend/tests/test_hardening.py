"""Edge cases: value precision, database-error mapping, upload limits, auth coverage, admin rules."""

import asyncio
import json
import logging
from datetime import datetime

import bcrypt
import pytest
import websockets
from app.api import realtime as realtime_api
from app.core.config import get_settings
from app.core.security import _dummy_hash
from app.db.session import SessionLocal, is_conflict
from app.models import Record, Role, User
from app.schemas.admin import UserAdminUpdate
from app.services import excel, records, users
from openpyxl import Workbook
from sqlalchemy import func, select, true
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

BODY = {"title": "Precision check", "value": 12345.678, "category": "cpu_temp", "recorded_at": "2026-10-01T08:00:00Z", "server": "S0001"}
# The display language is public on purpose: the login page shows it before anyone signs in.
PUBLIC = {("POST", "/api/v1/auth/register"), ("POST", "/api/v1/auth/login"), ("GET", "/health"), ("GET", "/health/live"),
          ("GET", "/api/v1/settings/display")}


def driver_error(kind, code: int, message: str):
    from asyncmy import errors

    return kind("statement", {}, getattr(errors, kind.__name__)(code, message))


async def test_values_come_back_exactly_as_stored(client, accounts):
    created = (await client.post("/api/v1/records", json=BODY, headers=accounts.engineer.headers)).json()
    stored = (await client.get(f"/api/v1/records/{created['id']}", headers=accounts.engineer.headers)).json()
    assert created["value"] == stored["value"] == 12345.678


async def test_a_value_the_database_refuses_is_a_422_not_a_500(client, accounts, monkeypatch):
    async def out_of_range(_session):
        raise driver_error(DataError, 1264, "Out of range value for column 'value'")

    monkeypatch.setattr(records, "list_categories", out_of_range)
    response = await client.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    assert response.status_code == 422 and response.json()["detail"] == "A value is out of range for its field"


async def test_a_write_conflict_is_a_409_with_retry_after(client, accounts, monkeypatch):
    async def deadlock(_session):
        raise driver_error(OperationalError, 1213, "Deadlock found when trying to get lock")

    monkeypatch.setattr(records, "list_categories", deadlock)
    response = await client.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    assert response.status_code == 409 and response.headers["Retry-After"] == "1"


async def test_an_upload_without_content_length_is_stopped_at_the_limit(client):
    sent = 0

    async def twenty_megabyte_file():
        nonlocal sent
        yield b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="big.csv"\r\nContent-Type: text/csv\r\n\r\n'
        for _ in range(320):
            sent += 65536
            yield b"x" * 65536
        yield b"\r\n--xyz--\r\n"

    # Without a token too: the limit applies before the form is parsed and before authentication.
    headers = {"Content-Type": "multipart/form-data; boundary=xyz"}
    response = await client.post("/api/v1/records/import", content=twenty_megabyte_file(), headers=headers)
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert sent < 4 * 1024 * 1024  # the server stopped reading near 2 MB instead of taking all 20 MB


@pytest.mark.parametrize(("name", "content", "message"), [
    ("rows.csv", b"\xff\xfe\x00bad", "UTF-8"),
    ("rows.csv", b"title,value,category,recorded_at,server\n", "no records"),
    ("rows.csv", b"title,value,category,recorded_at,server\n" + b"t,1,cpu_temp,2026-10-01T08:00:00Z,S0001\n" * 5001, "limit is 5000"),
    ("rows.csv", b"title,value,category,recorded_at,server\n" + b"x" * 200_000 + b",1,cpu_temp,2026-10-01T08:00:00Z,S0001\n", "Invalid CSV"),
    ("rows.json", b"[" * 100_000 + b"]" * 100_000, "nested too deeply"),
    ("rows.csv", b"x" * 200_000 + b"\n", "Invalid CSV"),
    ("rows.json", b'[{"value": ' + b"1" * 5000 + b"}]", "number is too long"),
])
async def test_import_rejects_files_it_cannot_take(client, accounts, name, content, message):
    files = {"file": (name, content, "application/json" if name.endswith(".json") else "text/csv")}
    response = await client.post("/api/v1/records/import", files=files, headers=accounts.engineer.headers)
    assert response.status_code == 422 and message in response.json()["detail"]


async def test_every_protected_route_needs_a_token(app, client):
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        url = path.replace("{record_id}", "1").replace("{user_id}", "1")
        for method in operations:
            if (method.upper(), path) in PUBLIC:
                continue
            response = await client.request(method.upper(), url)
            assert response.status_code == 401, f"{method.upper()} {path} answered {response.status_code}"
            checked += 1
    assert checked >= 20


async def test_a_websocket_recheck_that_hits_a_bug_closes_the_stream(app, live_server, accounts, monkeypatch):
    real_check, calls = realtime_api.load_active_user, []

    async def broken_after_connect(session, token):
        calls.append(1)
        if len(calls) == 1:
            return await real_check(session, token)
        raise driver_error(OperationalError, 1054, "Unknown column 'no_such_column'")

    monkeypatch.setattr(realtime_api, "load_active_user", broken_after_connect)
    monkeypatch.setattr(realtime_api, "AUTH_RECHECK_SECONDS", 0.05)
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    async with websockets.connect(f"{live_server}?token={token}") as ws:
        with pytest.raises(websockets.ConnectionClosed):
            for _ in range(50):
                await asyncio.sleep(0.05)
                app.state.hub.tick()
                json.loads(await asyncio.wait_for(ws.recv(), timeout=2))


async def test_idempotency_keys_are_case_sensitive(client, accounts):
    for key, value in (("AbC", 1.0), ("abc", 2.0)):
        headers = accounts.engineer.headers | {"Idempotency-Key": key}
        response = await client.post("/api/v1/records", json=BODY | {"value": value}, headers=headers)
        assert response.status_code == 201 and response.json()["value"] == value


@pytest.mark.usefixtures("accounts")  # an "engineer" account exists, so the insert below collides
async def test_database_errors_do_not_carry_the_values_sent():
    with pytest.raises(IntegrityError) as caught:
        async with SessionLocal() as session:
            session.add(User(username="engineer", email="private-address@example.com", hashed_password="x", role=Role.user))
            await session.commit()
    assert "private-address" not in str(caught.value)


async def test_the_last_active_admin_cannot_be_removed_by_two_admins_at_once(accounts, monkeypatch):
    # Two admins demote each other at the same time. The barrier holds both until each has loaded
    # its target, so both reach the locking read of the admin rows with two admins visible; without
    # `with_for_update` both would pass and no admin would remain.
    async with SessionLocal() as session:
        second = User(username="admin2", email="admin2@example.com", hashed_password="x", role=Role.admin)
        session.add(second)
        await session.commit()
    barrier = asyncio.Barrier(2)
    load = AsyncSession.get

    async def load_then_wait(self, *args, **kwargs):
        user = await load(self, *args, **kwargs)
        await barrier.wait()
        return user

    async def demote(actor, target_id):
        async with SessionLocal() as session:
            await users.update_user(session, actor, target_id, UserAdminUpdate(role=Role.user))

    monkeypatch.setattr(AsyncSession, "get", load_then_wait)
    race = asyncio.gather(demote(accounts.admin.user, second.id), demote(second, accounts.admin.user.id), return_exceptions=True)
    outcomes = await asyncio.wait_for(race, 10)  # a side that fails before the barrier must not hang the suite
    monkeypatch.setattr(AsyncSession, "get", load)
    refused = [o for o in outcomes if isinstance(o, Exception)]
    assert len(refused) == 1, outcomes
    # Either the second reads one admin (lock released after the first commit) or, under MariaDB's
    # snapshot isolation, its locking read reports the row changed since its snapshot (1020 -> 409).
    assert isinstance(refused[0], users.ValidationFailedError) or (isinstance(refused[0], DBAPIError) and is_conflict(refused[0]))
    async with SessionLocal() as session:
        active = await session.scalar(select(func.count()).select_from(User).where(User.role == Role.admin, User.is_active == true()))
    assert active == 1


async def test_a_failed_login_does_not_log_what_was_typed(client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.users"):
        await client.post("/api/v1/auth/login", data={"username": "MyS3cretPassw0rd", "password": "x"})
    assert "Login failed: unknown username" in caplog.text and "MyS3cretPassw0rd" not in caplog.text


def test_the_dummy_hash_costs_as_much_as_a_real_one():
    rounds = int(_dummy_hash().split("$")[2])
    assert rounds == get_settings().bcrypt_rounds
    assert _dummy_hash().startswith(bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=rounds)).decode()[:7])


def test_excel_stores_formula_looking_text_as_text():
    sheet = Workbook().active
    cell = excel._text(sheet, '=HYPERLINK("x")')
    assert cell.data_type == "s" and cell.value == '=HYPERLINK("x")'  # the text itself is not altered
    assert excel._text(sheet, "-40C cold start") == "-40C cold start"
    assert excel._text(sheet, "+1 retest") == "+1 retest"


async def test_a_log_line_from_a_worker_thread_reaches_the_system_log(app, client, accounts):

    await asyncio.to_thread(logging.getLogger("app.test").info, "from a worker thread")
    await asyncio.sleep(0.05)  # the loop runs the hand-over
    await app.state.log_writer.flush()
    logs = (await client.get("/api/v1/admin/logs", params={"q": "from a worker thread"}, headers=accounts.admin.headers)).json()
    assert [item["message"] for item in logs["items"]] == ["from a worker thread"]


async def test_an_unknown_query_parameter_is_refused(client, accounts):
    headers = accounts.viewer.headers
    misspelt = await client.get("/api/v1/records", params={"catgory": "cpu_temp"}, headers=headers)
    assert misspelt.status_code == 422
    assert misspelt.json()["errors"] == [{"field": "query.catgory", "message": "Unknown query parameter"}]
    paged = await client.get("/api/v1/records", params={"category": "cpu_temp", "page": 1, "page_size": 5}, headers=headers)
    assert paged.status_code == 200  # parameters of shared dependencies (paging) are known too
    assert (await client.get("/api/v1/analytics/trend", params={"bucket": "hour"}, headers=headers)).status_code == 422
    for bad in ("NaN", "Infinity", "-Infinity", "1e400", "2e15"):  # the database cannot compare with these
        assert (await client.get("/api/v1/records", params={"min_value": bad}, headers=headers)).status_code == 422, bad
        assert (await client.get("/api/v1/records", params={"max_value": bad}, headers=headers)).status_code == 422, bad
    # Without a token the answer is 401, so the accepted names are not revealed to anyone signed out.
    assert (await client.get("/api/v1/records", params={"catgory": "x"})).status_code == 401
    assert (await client.get("/api/v1/settings/display", params={"lang": "en"})).status_code == 422


async def test_a_misspelt_body_field_is_refused_not_ignored(client, accounts):
    headers = accounts.engineer.headers
    record = await client.post("/api/v1/records", json={"title": "edge case", "value": 1, "category": "cpu_temp", "server": "S0001",
                                                         "recorded_at": "2026-10-01T08:00:00Z"}, headers=headers)
    typo = await client.patch(f"/api/v1/records/{record.json()['id']}", json={"titel": "renamed"}, headers=headers)
    assert typo.status_code == 422 and typo.json()["errors"][0]["field"] == "titel"
    assert (await client.get(f"/api/v1/records/{record.json()['id']}", headers=headers)).json()["title"] == "edge case"
    csv = b"title,value,category,recorded_at,server,note\nA,1,cpu_temp,2026-10-01T08:00:00Z,S0001,x\n"
    extra = await client.post("/api/v1/records/import", files={"file": ("rows.csv", csv, "text/csv")}, headers=headers)
    assert extra.status_code == 422 and extra.json()["detail"] == "CSV has unknown columns: note"
    empty = await client.post("/api/v1/records/import", files={"file": ("rows.csv", b"", "text/csv")}, headers=headers)
    assert empty.status_code == 422 and empty.json()["detail"] == "File contains no records"


async def test_category_filters_ignore_case(client, accounts):
    headers = accounts.viewer.headers
    await client.post("/api/v1/records", json={"title": "edge case", "value": 1, "category": "cpu_temp", "server": "S0001",
                                               "recorded_at": "2026-10-01T08:00:00Z"}, headers=accounts.engineer.headers)
    upper = (await client.get("/api/v1/records", params={"category": "CPU_TEMP"}, headers=headers)).json()
    assert upper["total"] == 1


async def test_a_control_character_cannot_break_the_excel_report(client, accounts):
    body = {"title": "bad\u0001title", "value": 1, "category": "cpu_temp", "server": "S0001", "recorded_at": "2026-10-01T08:00:00Z"}
    refused = await client.post("/api/v1/records", json=body, headers=accounts.engineer.headers)
    assert refused.status_code == 422 and refused.json()["errors"][0]["message"] == "title must not contain control characters"
    async with SessionLocal() as session:  # a row stored before the rule existed
        session.add(Record(title="old\u0000row", value=1, category="cpu_temp", server="S0001",
                           recorded_at=datetime(2026, 10, 1, 8), owner_id=accounts.engineer.user.id))
        await session.commit()
    report = await client.get("/api/v1/analytics/export", headers=accounts.viewer.headers)
    assert report.status_code == 200
    csv = b"title,value,category,recorded_at,server,value\nA,1,cpu_temp,2026-10-01T08:00:00Z,S0001,2\n"
    twice = await client.post("/api/v1/records/import", files={"file": ("rows.csv", csv, "text/csv")}, headers=accounts.engineer.headers)
    assert twice.status_code == 422 and twice.json()["detail"] == "CSV has a column twice"
    # An unquoted comma in a title shifts the cells right; the row is refused, not imported shifted.
    shifted = b"title,value,category,recorded_at,server\nBurn-in, rack 3,1,cpu_temp,2026-10-01T08:00:00Z,S0001\n"
    refused = await client.post("/api/v1/records/import", files={"file": ("rows.csv", shifted, "text/csv")},
                                headers=accounts.engineer.headers)
    assert refused.status_code == 422
    assert refused.json()["errors"] == [{"row": 2, "field": None,
                                         "message": "Row has 1 more cell(s) than the header; put values with commas in quotes"}]
