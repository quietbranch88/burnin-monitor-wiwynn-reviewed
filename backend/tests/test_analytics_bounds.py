"""The documented one-hour default must be representable before querying telemetry."""

import pytest


@pytest.mark.parametrize("endpoint", ["summary", "categories", "trend", "export"])
@pytest.mark.parametrize("end", ["0001-01-01T00:00:00Z", "0001-01-01T00:59:59.999999Z"])
async def test_one_server_default_window_underflow_is_a_validation_error(client, accounts, endpoint, end):
    response = await client.get(
        f"/api/v1/analytics/{endpoint}",
        params={"source": "realtime", "server": "S0001", "end": end},
        headers=accounts.viewer.headers,
    )
    assert response.status_code == 422, response.text
    assert any(error["field"] == "query.end" for error in response.json()["errors"])


@pytest.mark.parametrize("bounds", [
    {"end": "0001-01-01T01:00:00Z"},
    {"start": "0001-01-01T00:00:00Z", "end": "0001-01-01T00:00:00Z"},
])
async def test_one_server_representable_extreme_window_is_still_accepted(client, accounts, bounds):
    response = await client.get(
        "/api/v1/analytics/summary",
        params={"source": "realtime", "server": "S0001", **bounds},
        headers=accounts.viewer.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 0
