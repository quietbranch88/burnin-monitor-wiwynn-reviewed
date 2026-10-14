from tests.conftest import create_record


async def test_create_normalises_input_and_converts_to_utc(client, accounts):
    body = await create_record(
        client, accounts.engineer, title="  Line 2 station 05  ", category="CPU_Temp", recorded_at="2026-10-01T16:00:00+08:00"
    )
    assert body["title"] == "Line 2 station 05"
    assert body["category"] == "cpu_temp"
    assert body["recorded_at"] == "2026-10-01T08:00:00Z"
    assert body["owner_username"] == "engineer"


async def test_viewer_cannot_create(client, accounts):
    response = await client.post(
        "/api/v1/records",
        json={"title": "x", "value": 1, "category": "cpu_temp", "recorded_at": "2026-10-01T00:00:00Z", "server": "S0001"},
        headers=accounts.viewer.headers,
    )
    assert response.status_code == 403


async def test_create_validation(client, accounts):
    bad = {"title": "   ", "value": "abc", "category": "Has Space", "recorded_at": "soon", "server": "S 01"}
    response = await client.post("/api/v1/records", json=bad, headers=accounts.engineer.headers)
    assert response.status_code == 422
    assert {e["field"] for e in response.json()["errors"]} == {"title", "value", "category", "recorded_at", "server"}


async def test_list_pages_filters_and_sorts(client, accounts):
    for i, (category, value) in enumerate([("cpu_temp", 60), ("cpu_temp", 95), ("power_w", 700), ("cpu_temp", 75)]):
        await create_record(
            client, accounts.engineer, title=f"Station {i}", category=category, value=value,
            recorded_at=f"2026-10-0{i + 1}T00:00:00Z",
        )
    await create_record(client, accounts.admin, title="Admin station", value=80, recorded_at="2026-10-09T00:00:00Z")
    url = "/api/v1/records"
    headers = accounts.viewer.headers

    page = (await client.get(url, params={"page_size": 2, "page": 2}, headers=headers)).json()
    assert (page["total"], page["pages"], len(page["items"])) == (5, 3, 2)

    cpu = (await client.get(url, params={"category": "cpu_temp", "sort": "value", "order": "asc"}, headers=headers)).json()
    assert [r["value"] for r in cpu["items"]] == [60, 75, 80, 95]

    ranged = (await client.get(url, params={"min_value": 70, "max_value": 90}, headers=headers)).json()
    assert sorted(r["value"] for r in ranged["items"]) == [75, 80]

    window = await client.get(
        url, params={"start": "2026-10-02T00:00:00Z", "end": "2026-10-03T00:00:00Z", "sort": "recorded_at", "order": "asc"},
        headers=headers,
    )
    assert [r["title"] for r in window.json()["items"]] == ["Station 1", "Station 2"]

    search = (await client.get(url, params={"q": "admin"}, headers=headers)).json()
    assert [r["title"] for r in search["items"]] == ["Admin station"]

    mine = (await client.get(url, params={"mine": True}, headers=accounts.admin.headers)).json()
    assert mine["total"] == 1

    assert (await client.get(url, params={"sort": "hashed_password"}, headers=headers)).status_code == 422
    assert (await client.get(url, params={"page_size": 500}, headers=headers)).status_code == 422


async def test_pages_partition_the_sorted_list_without_gaps_or_repeats(client, accounts):
    # Seven records, three with the same value: a page that ignored its offset, or an unstable
    # order among equal values, would repeat or skip ids across the pages.
    for value in (70, 50, 70, 90, 70, 60, 80):
        await create_record(client, accounts.engineer, value=value)
    url, headers = "/api/v1/records", accounts.viewer.headers
    for order in ("asc", "desc"):
        pages = [(await client.get(url, params={"sort": "value", "order": order, "page_size": 3, "page": p}, headers=headers)).json()
                 for p in (1, 2, 3, 4)]
        assert [len(p["items"]) for p in pages] == [3, 3, 1, 0]  # the last page is short, past the end is empty
        assert {(p["total"], p["pages"]) for p in pages} == {(7, 3)}
        rows = [(r["value"], r["id"]) for p in pages for r in p["items"]]
        assert rows == sorted(rows, reverse=order == "desc")  # value, then id, across page boundaries
        assert len({row_id for _, row_id in rows}) == 7


async def test_categories_lists_distinct_values(client, accounts):
    await create_record(client, accounts.engineer, category="power_w")
    await create_record(client, accounts.engineer, category="cpu_temp")
    await create_record(client, accounts.engineer, category="cpu_temp")
    response = await client.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    assert response.json() == ["cpu_temp", "power_w"]


async def test_update_and_delete_are_limited_to_owner_or_admin(client, accounts):
    record = await create_record(client, accounts.engineer)
    path = f"/api/v1/records/{record['id']}"

    assert (await client.patch(path, json={"value": 1}, headers=accounts.other.headers)).status_code == 403
    assert (await client.patch(path, json={"value": 1}, headers=accounts.viewer.headers)).status_code == 403
    assert (await client.delete(path, headers=accounts.other.headers)).status_code == 403

    own = await client.patch(path, json={"value": 72.5}, headers=accounts.engineer.headers)
    assert own.status_code == 200 and own.json()["value"] == 72.5 and own.json()["title"] == record["title"]

    by_admin = await client.patch(path, json={"title": "Reviewed"}, headers=accounts.admin.headers)
    assert by_admin.status_code == 200 and by_admin.json()["title"] == "Reviewed"

    assert (await client.delete(path, headers=accounts.admin.headers)).status_code == 204
    assert (await client.get(path, headers=accounts.viewer.headers)).status_code == 404


async def test_owner_can_delete_own_record(client, accounts):
    record = await create_record(client, accounts.engineer)
    path = f"/api/v1/records/{record['id']}"
    assert (await client.delete(path, headers=accounts.engineer.headers)).status_code == 204
    assert (await client.delete(path, headers=accounts.engineer.headers)).status_code == 404


async def test_creates_updates_and_deletes_are_in_the_system_log(app, client, accounts):
    headers = accounts.engineer.headers
    body = {"title": "Audit me", "value": 1, "category": "cpu_temp", "recorded_at": "2026-10-01T08:00:00Z", "server": "S0001"}
    record_id = (await client.post("/api/v1/records", json=body, headers=headers)).json()["id"]
    keyed = await client.post("/api/v1/records", json=body | {"title": "Keyed"}, headers=headers | {"Idempotency-Key": "audit-1"})
    await client.post("/api/v1/records", json=body | {"title": "Keyed"}, headers=headers | {"Idempotency-Key": "audit-1"})  # replay
    await client.patch(f"/api/v1/records/{record_id}", json={"value": 2, "title": "Audited"}, headers=headers)
    await client.delete(f"/api/v1/records/{record_id}", headers=headers)
    await app.state.log_writer.flush()
    logs = (await client.get("/api/v1/admin/logs", params={"q": "Record ", "page_size": 50}, headers=accounts.admin.headers)).json()
    # Other tests' lines may still be in the log writer's buffer; look at this test's records only.
    mine = {f"Record {record_id} ", f"Record {keyed.json()['id']} "}
    lines = sorted(m for m in (item["message"] for item in logs["items"]) if any(m.startswith(p) for p in mine))
    assert lines == sorted([
        f"Record {record_id} created by engineer",
        f"Record {keyed.json()['id']} created by engineer",  # once: the replay is not a new record
        f"Record {record_id} updated by engineer: title, value",
        f"Record {record_id} deleted by engineer",
    ])


def test_the_token_carries_no_role():
    import jwt
    from app.core.config import get_settings
    from app.core.security import create_access_token

    token, _ = create_access_token(7, 3)
    claims = jwt.decode(token, get_settings().jwt_secret, algorithms=[get_settings().jwt_algorithm])
    assert {"sub", "ver", "iat", "exp"} == set(claims) and claims["sub"] == "7" and claims["ver"] == 3


async def test_records_belong_to_a_server(client, accounts):
    headers = accounts.engineer.headers
    body = {"title": "No server", "value": 1, "category": "cpu_temp", "recorded_at": "2026-10-01T08:00:00Z"}
    missing = await client.post("/api/v1/records", json=body, headers=headers)
    assert missing.status_code == 422 and missing.json()["errors"][0]["field"] == "server"
    assert (await client.post("/api/v1/records", json=body | {"server": "  "}, headers=headers)).status_code == 422
    first = (await client.post("/api/v1/records", json=body | {"server": " S0017 "}, headers=headers)).json()
    await client.post("/api/v1/records", json=body | {"server": "S0018"}, headers=headers)
    assert first["server"] == "S0017"
    listed = (await client.get("/api/v1/records", params={"server": "S0017"}, headers=headers)).json()
    assert [r["server"] for r in listed["items"]] == ["S0017"]
    cleared = await client.patch(f"/api/v1/records/{first['id']}", json={"server": None}, headers=headers)
    assert cleared.status_code == 422  # a record cannot lose its server
    moved = await client.patch(f"/api/v1/records/{first['id']}", json={"server": "S0019"}, headers=headers)
    assert moved.json()["server"] == "S0019"
    csv = b"title,value,category,recorded_at\nA,1,cpu_temp,2026-10-01T08:00:00Z\n"
    no_column = await client.post("/api/v1/records/import", files={"file": ("rows.csv", csv, "text/csv")}, headers=headers)
    assert no_column.status_code == 422 and "server" in no_column.json()["detail"]
