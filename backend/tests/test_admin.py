from datetime import datetime

import pytest
from app.db.session import SessionLocal
from app.models import RealtimeReading

ADMIN_GETS = ["/api/v1/admin/users", "/api/v1/admin/logs", "/api/v1/admin/db-status", "/api/v1/admin/realtime/history"]


async def test_admin_endpoints_require_admin(client, accounts):
    for path in ADMIN_GETS:
        for account in (accounts.engineer, accounts.viewer):
            assert (await client.get(path, headers=account.headers)).status_code == 403, path
        assert (await client.get(path, headers=accounts.admin.headers)).status_code == 200, path
    patch = await client.patch(f"/api/v1/admin/users/{accounts.viewer.user.id}", json={"role": "admin"}, headers=accounts.engineer.headers)
    assert patch.status_code == 403


async def test_list_and_update_users(client, accounts):
    listed = (await client.get("/api/v1/admin/users", headers=accounts.admin.headers)).json()
    assert listed["total"] == 4 and {u["username"] for u in listed["items"]} == {"admin", "engineer", "other", "viewer"}

    path = f"/api/v1/admin/users/{accounts.viewer.user.id}"
    promoted = await client.patch(path, json={"role": "user"}, headers=accounts.admin.headers)
    assert promoted.status_code == 200 and promoted.json()["role"] == "user"
    # The role is read from the database, so the existing token gains the new rights.
    created = await client.post(
        "/api/v1/records",
        json={"title": "Now allowed", "value": 1, "category": "cpu_temp", "recorded_at": "2026-10-01T00:00:00Z", "server": "S0001"},
        headers=accounts.viewer.headers,
    )
    assert created.status_code == 201

    disabled = await client.patch(path, json={"is_active": False}, headers=accounts.admin.headers)
    assert disabled.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=accounts.viewer.headers)).status_code == 401


async def test_admin_cannot_lock_themselves_out(client, accounts):
    path = f"/api/v1/admin/users/{accounts.admin.user.id}"
    assert (await client.patch(path, json={"role": "viewer"}, headers=accounts.admin.headers)).status_code == 422
    assert (await client.patch(path, json={"is_active": False}, headers=accounts.admin.headers)).status_code == 422
    assert (await client.patch("/api/v1/admin/users/999999", json={"role": "user"}, headers=accounts.admin.headers)).status_code == 404


async def test_system_logs_record_requests_and_events(app, client, accounts):
    await client.get("/api/v1/records", headers=accounts.engineer.headers)
    await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "wrong-password"})
    await app.state.log_writer.flush()

    requests = (await client.get("/api/v1/admin/logs", params={"path": "/api/v1/records"}, headers=accounts.admin.headers)).json()
    entry = requests["items"][0]
    assert entry["method"] == "GET" and entry["status_code"] == 200
    assert entry["user_id"] == accounts.engineer.user.id and entry["duration_ms"] >= 0 and entry["request_id"]

    warnings = (await client.get("/api/v1/admin/logs", params={"level": "WARNING", "q": "Login failed"}, headers=accounts.admin.headers)).json()
    assert warnings["total"] == 1 and warnings["items"][0]["logger"] == "app.users"

    unauthorized = (await client.get("/api/v1/admin/logs", params={"status_code": 401}, headers=accounts.admin.headers)).json()
    assert unauthorized["total"] >= 1


async def test_database_status(client, accounts):
    status = (await client.get("/api/v1/admin/db-status", headers=accounts.admin.headers)).json()
    assert status["dialect"] == "mariadb+asyncmy"
    assert status["server_version"].startswith("11.7")
    assert status["pool"]["configured_size"] == 10 and status["pool"]["checked_out"] >= 1
    assert set(status["table_rows"]) == {"users", "records", "realtime_minute_stats", "realtime_alerts", "realtime_readings",
                                         "system_logs"}
    assert status["approximate_rows"] == ["realtime_readings", "system_logs"]
    assert status["table_rows"]["users"] == 4
    assert {"pending", "written", "last_flush_at"} <= set(status["realtime_writer"])


async def test_realtime_history_pages_by_cursor(client, accounts):
    rows = [("S0001", "cpu_temp", "CPU0", 80, 90), ("S0002", "cpu_temp", "CPU1", 95, 90),
            ("S0001", "power_w", "PSU0", 600, 2700), ("S0002", "cpu_temp", "CPU0", 99, 90)]
    async with SessionLocal() as session:
        session.add_all(
            RealtimeReading(server=srv, category=c, sensor=s, value=v, threshold=t, is_anomaly=v > t,
                            generated_at=datetime(2026, 10, 1, 0, 0, i))
            for i, (srv, c, s, v, t) in enumerate(rows)
        )
        await session.commit()
    headers = accounts.admin.headers
    url = "/api/v1/admin/realtime/history"
    first = (await client.get(url, params={"limit": 2}, headers=headers)).json()
    assert [r["value"] for r in first["items"]] == [99, 600] and first["next_before_id"] is not None
    cursor = {"before_at": first["next_before_at"], "before_id": first["next_before_id"]}
    second = (await client.get(url, params={"limit": 2, **cursor}, headers=headers)).json()
    assert [r["value"] for r in second["items"]] == [95, 80] and second["next_before_id"] is None
    assert (await client.get(url, params={"before_id": first["next_before_id"]}, headers=headers)).status_code == 422  # half a cursor
    alerts = (await client.get(url, params={"anomalies_only": True}, headers=headers)).json()
    assert [r["value"] for r in alerts["items"]] == [99, 95]
    one_server = (await client.get(url, params={"server": "S0001"}, headers=headers)).json()
    assert {r["server"] for r in one_server["items"]} == {"S0001"} and len(one_server["items"]) == 2
    window = await client.get(
        url, params={"category": "cpu_temp", "start": "2026-10-01T00:00:01Z", "end": "2026-10-01T00:00:01Z"}, headers=headers
    )
    assert [r["value"] for r in window.json()["items"]] == [95]
    one_sensor = (await client.get(url, params={"category": "CPU_Temp", "sensor": "CPU0"}, headers=headers)).json()
    assert [(r["server"], r["sensor"], r["value"]) for r in one_sensor["items"]] == [("S0002", "CPU0", 99), ("S0001", "CPU0", 80)]


async def test_overlong_http_method_does_not_block_system_logs(app, client, accounts):
    response = await client.request("UNSUBSCRIBE", "/api/v1/records")
    assert response.status_code == 405
    await app.state.log_writer.flush()
    assert app.state.log_writer.last_error is None
    logs = (await client.get("/api/v1/admin/logs", params={"status_code": 405}, headers=accounts.admin.headers)).json()
    assert logs["items"][0]["method"] == "UNSUBSCRIB"


async def test_history_end_time_includes_readings_written_late(client, accounts):
    base = datetime(2026, 10, 1, 0, 0, 0)
    async with SessionLocal() as session:
        for second, server in ((0, "S0001"), (10, "S0002"), (20, "S0003")):
            session.add(RealtimeReading(server=server, category="cpu_temp", sensor="CPU0", value=1, threshold=90,
                                        is_anomaly=False, generated_at=base.replace(second=second)))
        await session.flush()
        # Stored last but generated before the cut: a reading written late, after an outage.
        session.add(RealtimeReading(server="S0004", category="cpu_temp", sensor="CPU0", value=1, threshold=90,
                                    is_anomaly=False, generated_at=base.replace(second=5)))
        await session.commit()
    url, headers = "/api/v1/admin/realtime/history", accounts.admin.headers
    response = await client.get(url, params={"end": "2026-10-01T00:00:10Z"}, headers=headers)
    assert response.status_code == 200
    page = response.json()
    # The end boundary is inclusive and applies to generation time, even after a delayed write,
    # and the late reading sorts at its own time, between the readings around it.
    assert [r["server"] for r in page["items"]] == ["S0002", "S0004", "S0001"]
    assert page["next_before_id"] is None
    assert "S0004" in [r["server"] for r in (await client.get(url, headers=headers)).json()["items"]]
    nothing = (await client.get(url, params={"end": "2025-01-01T00:00:00Z"}, headers=headers)).json()
    assert nothing == {"items": [], "next_before_at": None, "next_before_id": None}


@pytest.mark.parametrize("page_size", [1, 2, 3])
async def test_history_filtered_pages_include_all_matching_delayed_readings(client, accounts, page_size):
    # Explicit insertion order differs from event-time order. Values identify rows independently
    # of generated IDs, with matching rows at both inclusive bounds and several distractors.
    rows = [
        (5, "S0001", "cpu_temp", "CPU0", True, 101),
        (10, "S0001", "cpu_temp", "CPU1", True, 102),
        (7, "S0001", "power_w", "CPU0", True, 103),
        (8, "S0002", "cpu_temp", "CPU0", True, 104),
        (6, "S0001", "cpu_temp", "CPU0", True, 105),
        (9, "S0001", "cpu_temp", "CPU0", False, 86),
        (10, "S0001", "cpu_temp", "CPU0", True, 107),
        (4, "S0001", "cpu_temp", "CPU0", True, 108),
        (11, "S0001", "cpu_temp", "CPU0", True, 109),
        (7, "S0001", "cpu_temp", "CPU0", True, 110),
    ]
    async with SessionLocal() as session:
        for second, server, category, sensor, anomaly, value in rows:
            session.add(RealtimeReading(
                server=server, category=category, sensor=sensor, value=value, threshold=90,
                is_anomaly=anomaly, generated_at=datetime(2026, 10, 1, 0, 0, second),
            ))
            await session.flush()
        await session.commit()

    params = {"start": "2026-10-01T00:00:05Z", "end": "2026-10-01T00:00:10Z",
              "category": "CPU_Temp", "server": "S0001", "sensor": "CPU0",
              "anomalies_only": True, "limit": page_size}
    expected_values = [107, 110, 105, 101]  # newest generation time first: seconds 10, 7, 6, 5
    observed_ids = []
    for offset in range(0, len(expected_values), page_size):
        response = await client.get("/api/v1/admin/realtime/history", params=params, headers=accounts.admin.headers)
        assert response.status_code == 200
        page = response.json()
        assert [row["value"] for row in page["items"]] == expected_values[offset:offset + page_size]
        observed_ids.extend(row["id"] for row in page["items"])
        if offset + page_size < len(expected_values):
            last = page["items"][-1]
            assert (page["next_before_at"], page["next_before_id"]) == (last["generated_at"], last["id"])
            params["before_at"], params["before_id"] = page["next_before_at"], page["next_before_id"]
        else:
            assert page["next_before_id"] is None and page["next_before_at"] is None
    assert len(set(observed_ids)) == len(observed_ids) == 4  # every matching row once, late-stored ones included


async def test_history_cursor_keeps_readings_with_tied_timestamps(client, accounts):
    rows = [(10, "S0001", "CPU0"), (10, "S0002", "CPU0"),
            (10, "S0001", "CPU1"), (5, "S0003", "CPU0")]
    async with SessionLocal() as session:
        for second, server, sensor in rows:
            session.add(RealtimeReading(
                server=server, category="cpu_temp", sensor=sensor, value=80, threshold=90,
                is_anomaly=False, generated_at=datetime(2026, 10, 1, 0, 0, second),
            ))
            await session.flush()
        await session.commit()
    url = "/api/v1/admin/realtime/history"
    params = {"end": "2026-10-01T00:00:10Z", "limit": 2}
    first_response = await client.get(url, params=params, headers=accounts.admin.headers)
    assert first_response.status_code == 200
    first = first_response.json()
    # Three readings share second 10: time first, then the newest id, across the page boundary.
    assert [(row["server"], row["sensor"]) for row in first["items"]] == [("S0001", "CPU1"), ("S0002", "CPU0")]
    assert (first["next_before_at"], first["next_before_id"]) == (first["items"][-1]["generated_at"], first["items"][-1]["id"])
    cursor = {"before_at": first["next_before_at"], "before_id": first["next_before_id"]}
    second_response = await client.get(url, params=params | cursor, headers=accounts.admin.headers)
    assert second_response.status_code == 200
    second = second_response.json()
    assert [(row["server"], row["sensor"]) for row in second["items"]] == [("S0001", "CPU0"), ("S0003", "CPU0")]
    assert second["next_before_id"] is None


async def test_an_admin_creates_accounts_and_resets_passwords(app, client, accounts):
    admin = accounts.admin.headers
    new = {"username": "line.lead", "email": "lead@example.com", "password": "Burnin2026x", "role": "user"}
    created = await client.post("/api/v1/admin/users", json=new, headers=admin)
    assert created.status_code == 201 and created.json()["role"] == "user"
    assert (await client.post("/api/v1/admin/users", json=new, headers=admin)).status_code == 409
    for account in (accounts.engineer, accounts.viewer):
        assert (await client.post("/api/v1/admin/users", json=new | {"username": "x.y", "email": "x@example.com"},
                                  headers=account.headers)).status_code == 403
    login = await client.post("/api/v1/auth/login", data={"username": "line.lead", "password": "Burnin2026x"})
    token = {"Authorization": f"Bearer {login.json()['access_token']}"}
    user_id = created.json()["id"]
    weak = await client.put(f"/api/v1/admin/users/{user_id}/password", json={"password": "short"}, headers=admin)
    assert weak.status_code == 422
    assert (await client.put(f"/api/v1/admin/users/{user_id}/password", json={"password": "NewPass2026y"},
                             headers=accounts.engineer.headers)).status_code == 403
    reset = await client.put(f"/api/v1/admin/users/{user_id}/password", json={"password": "NewPass2026y"}, headers=admin)
    assert reset.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=token)).status_code == 401  # the old session ended
    assert (await client.post("/api/v1/auth/login", data={"username": "line.lead", "password": "Burnin2026x"})).status_code == 401
    assert (await client.post("/api/v1/auth/login", data={"username": "line.lead", "password": "NewPass2026y"})).status_code == 200
    assert (await client.put("/api/v1/admin/users/999999/password", json={"password": "NewPass2026y"}, headers=admin)).status_code == 404
    await app.state.log_writer.flush()
    logs = (await client.get("/api/v1/admin/logs", params={"logger": "app.users", "page_size": 50}, headers=admin)).json()
    messages = {item["message"] for item in logs["items"]}
    assert "Admin admin created line.lead (user)" in messages and "Admin admin reset the password of line.lead" in messages
    assert not any("NewPass2026y" in m or "Burnin2026x" in m for m in messages)


async def test_the_log_separates_events_from_request_lines(app, client, accounts):
    admin = accounts.admin.headers
    await client.get("/api/v1/records", headers=accounts.engineer.headers)  # a request line
    await client.patch(f"/api/v1/admin/users/{accounts.viewer.user.id}", json={"role": "user"}, headers=admin)  # an event
    await app.state.log_writer.flush()
    events = (await client.get("/api/v1/admin/logs", params={"kind": "events"}, headers=admin)).json()["items"]
    requests = (await client.get("/api/v1/admin/logs", params={"kind": "requests"}, headers=admin)).json()["items"]
    assert events and all(item["logger"] != "app.request" for item in events)
    assert any("changed viewer" in item["message"] for item in events)
    assert requests and all(item["logger"] == "app.request" for item in requests)
