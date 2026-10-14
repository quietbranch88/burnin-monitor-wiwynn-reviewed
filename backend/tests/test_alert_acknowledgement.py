"""Alert acknowledgement: roles, first confirmation wins, and the audit log entry."""

import asyncio
from datetime import datetime, timedelta

import pytest
from app.db.session import SessionLocal
from app.models import RealtimeAlert, Role, SystemLog, User
from app.services.alerts import acknowledge_alert_event
from sqlalchemy import event, select, update

URL = "/api/v1/realtime/alert-events"


async def add_alerts(count: int = 1) -> list[int]:
    base = datetime(2026, 10, 1, 8)
    async with SessionLocal() as session:
        rows = [RealtimeAlert(
            server="S0001", category="cpu_temp", sensor="CPU0", started_at=base + timedelta(seconds=i),
            ended_at=base + timedelta(seconds=i + 3), threshold=90, peak_value=97.5, readings=3,
        ) for i in range(count)]
        session.add_all(rows)
        await session.commit()
        return [row.id for row in rows]


async def acknowledgement_logs() -> list[SystemLog]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(SystemLog).where(SystemLog.logger == "app.alerts")))


async def test_viewer_and_anonymous_cannot_acknowledge_or_mutate_state(client, accounts):
    [event_id] = await add_alerts()
    for headers, status in ((accounts.viewer.headers, 403), ({}, 401)):
        response = await client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=headers)
        assert response.status_code == status
    async with SessionLocal() as session:
        row = await session.get(RealtimeAlert, event_id)
        assert row.acknowledged_at is None and row.acknowledged_by is None
    assert await acknowledgement_logs() == []


@pytest.mark.parametrize("role", ["admin", "engineer"])
async def test_editors_acknowledge_and_repeat_preserves_first_actor_time_and_one_audit(client, accounts, app, role):
    [event_id] = await add_alerts()
    actor = getattr(accounts, role)
    first = await client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=actor.headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["acknowledged_by"] == role and body["acknowledged_at"].endswith("Z")
    assert body["id"] == event_id and body["peak_value"] == 97.5
    repeated = await client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=accounts.other.headers)
    assert repeated.status_code == 200 and repeated.json() == body
    listed = await client.get(URL, headers=accounts.viewer.headers)
    assert listed.json()["items"] == [body]
    # Audit is already durable before the asynchronous request log buffer is flushed.
    logs = await acknowledgement_logs()
    assert len(logs) == 1 and logs[0].user_id == actor.user.id
    assert logs[0].message == f"Alert event {event_id} acknowledged by {role}"
    assert logs[0].request_id == first.headers["X-Request-ID"]
    async with SessionLocal() as session:
        row = await session.get(RealtimeAlert, event_id)
        assert row.acknowledged_by == actor.user.id
        assert row.acknowledged_at == logs[0].created_at
        assert row.acknowledged_at == datetime.fromisoformat(body["acknowledged_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    await app.state.log_writer.flush()
    assert len(await acknowledgement_logs()) == 1


async def test_acknowledgement_uses_current_database_role(client, accounts):
    [event_id] = await add_alerts()
    headers = accounts.engineer.headers
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.id == accounts.engineer.user.id).values(role=Role.viewer))
        await session.commit()
    response = await client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=headers)
    assert response.status_code == 403
    assert await acknowledgement_logs() == []


@pytest.mark.parametrize("body", [
    {}, {"acknowledged": False}, {"acknowledged": None}, {"acknowledged": 1},
    {"acknowledged": "true"}, {"acknowledged": True, "acknowledged_by": "admin"},
    {"acknowledged": True, "acknowledged_at": "2026-01-01T00:00:00Z"},
])
async def test_acknowledgement_accepts_only_true_and_no_extra_fields(client, accounts, body):
    [event_id] = await add_alerts()
    response = await client.patch(f"{URL}/{event_id}", json=body, headers=accounts.engineer.headers)
    assert response.status_code == 422
    async with SessionLocal() as session:
        row = await session.get(RealtimeAlert, event_id)
        assert row.acknowledged_at is None and row.acknowledged_by is None
    assert await acknowledgement_logs() == []


async def test_missing_alert_returns_404_without_an_audit(client, accounts):
    response = await client.patch(f"{URL}/999999999", json={"acknowledged": True}, headers=accounts.engineer.headers)
    assert response.status_code == 404
    assert await acknowledgement_logs() == []


async def test_acknowledged_filter_retains_unacknowledged_rows_and_cursor_order(client, accounts):
    ids = await add_alerts(5)
    for event_id in (ids[1], ids[3]):
        assert (await client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=accounts.engineer.headers)).status_code == 200
    headers = accounts.viewer.headers
    all_rows = (await client.get(URL, headers=headers)).json()["items"]
    assert [row["id"] for row in all_rows] == ids[::-1]
    confirmed = (await client.get(URL, params={"acknowledged": "true"}, headers=headers)).json()["items"]
    assert [row["id"] for row in confirmed] == [ids[3], ids[1]]
    assert all(row["acknowledged_by"] == "engineer" for row in confirmed)
    first = (await client.get(URL, params={"acknowledged": "false", "limit": 2}, headers=headers)).json()
    assert [row["id"] for row in first["items"]] == [ids[4], ids[2]]
    assert all(row["acknowledged_by"] is None and row["acknowledged_at"] is None for row in first["items"])
    second = (await client.get(URL, params={
        "acknowledged": "false", "limit": 2, "before_at": first["next_before_at"], "before_id": first["next_before_id"],
    }, headers=headers)).json()
    assert [row["id"] for row in second["items"]] == [ids[0]] and second["next_before_at"] is None
    assert (await client.get(URL, params={"acknowledged": "maybe"}, headers=headers)).status_code == 422


async def test_concurrent_acknowledgements_have_one_winner_and_one_audit(client, accounts, app):
    [event_id] = await add_alerts()
    responses = await asyncio.gather(*[
        client.patch(f"{URL}/{event_id}", json={"acknowledged": True}, headers=actor.headers)
        for actor in (accounts.engineer, accounts.other, accounts.admin, accounts.engineer)
    ])
    assert [response.status_code for response in responses] == [200] * 4, [response.text for response in responses]
    bodies = [response.json() for response in responses]
    assert all(body == bodies[0] for body in bodies)
    await app.state.log_writer.flush()
    logs = await acknowledgement_logs()
    assert len(logs) == 1
    async with SessionLocal() as session:
        row = await session.get(RealtimeAlert, event_id)
        actor = await session.get(User, row.acknowledged_by)
        assert actor.username == bodies[0]["acknowledged_by"]
        assert logs[0].user_id == actor.id and logs[0].created_at == row.acknowledged_at


async def test_audit_failure_rolls_back_acknowledgement(accounts):
    [event_id] = await add_alerts()
    async with SessionLocal() as session:
        def fail_audit_flush(sync_session, _flush_context, _instances):
            if any(isinstance(item, SystemLog) for item in sync_session.new):
                raise RuntimeError("controlled audit persistence failure")

        event.listen(session.sync_session, "before_flush", fail_audit_flush)
        with pytest.raises(RuntimeError, match="controlled audit persistence failure"):
            await acknowledge_alert_event(session, accounts.engineer.user, event_id)
        await session.rollback()
    async with SessionLocal() as session:
        row = await session.get(RealtimeAlert, event_id)
        assert row.acknowledged_at is None and row.acknowledged_by is None
    assert await acknowledgement_logs() == []
