import random

import pytest
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import MetricThreshold, SystemSettings
from app.services import realtime as realtime_service
from app.services import site_settings
from app.services.realtime import RealtimeHub, generate_value, load
from sqlalchemy import select

URL = "/api/v1/admin/settings"


@pytest.fixture
async def hub(app):
    """The app's hub, put back to the .env settings afterwards: it lives for the whole test session."""
    hub = app.state.hub
    yield hub
    configured = get_settings()
    hub._settings_changed_at = None  # forget the test's save, or the undated reset below is ignored
    hub.apply_settings(dict(configured.realtime_thresholds), configured.realtime_warning_percent)


@pytest.mark.usefixtures("hub")
async def test_settings_start_from_env_values(client, accounts):
    body = (await client.get(URL, headers=accounts.admin.headers)).json()
    assert body["warning_percent"] == 90 and body["language"] == "en" and body["updated_by"] is None
    fan = next(t for t in body["thresholds"] if t["category"] == "fan_rpm")
    assert fan == {"category": "fan_rpm", "threshold": 3000.0, "default": 3000.0, "alert_below": True}
    assert body["time_zone"] == "Asia/Taipei"
    # No login needed: the login page shows the language and the plant's time.
    assert (await client.get("/api/v1/settings/display")).json() == {"language": "en", "time_zone": "Asia/Taipei"}


async def test_an_admin_change_applies_at_once_and_is_logged(app, client, accounts, hub):
    change = {"warning_percent": 80, "language": "zh", "time_zone": "America/Ciudad_Juarez",
              "thresholds": {"gpu_temp": 75, "fan_rpm": 3500}}
    body = (await client.patch(URL, json=change, headers=accounts.admin.headers)).json()
    assert body["warning_percent"] == 80 and body["language"] == "zh" and body["updated_by"] == "admin"
    assert {t["category"]: t["threshold"] for t in body["thresholds"]}["gpu_temp"] == 75
    # The running generator uses the new limit from its next tick.
    assert hub.thresholds["gpu_temp"] == 75 and hub.warning_percent == 80
    message = hub.tick()
    assert message["warning_percent"] == 80 and message["summary"]["gpu_temp"]["threshold"] == 75
    assert all(r["threshold"] == 75 and r["is_anomaly"] == (r["value"] > 75)
               for r in hub.last_readings if r["category"] == "gpu_temp")
    config = (await client.get("/api/v1/realtime/config", headers=accounts.viewer.headers)).json()
    assert config["thresholds"]["fan_rpm"] == 3500 and config["warning_percent"] == 80 and config["alert_below"] == ["fan_rpm"]
    assert (await client.get("/api/v1/settings/display")).json() == {"language": "zh", "time_zone": "America/Ciudad_Juarez"}
    await app.state.log_writer.flush()
    logs = (await client.get("/api/v1/admin/logs", params={"q": "changed settings"}, headers=accounts.admin.headers)).json()
    line = next(item for item in logs["items"] if "changed settings" in item["message"])
    assert "gpu_temp threshold 85 -> 75" in line["message"] and "warning 90% -> 80%" in line["message"]
    assert "time zone Asia/Taipei -> America/Ciudad_Juarez" in line["message"]


async def test_only_admins_change_settings(client, accounts, hub):
    change = {"thresholds": {"gpu_temp": 70}}
    for account in (accounts.engineer, accounts.viewer):
        assert (await client.patch(URL, json=change, headers=account.headers)).status_code == 403
        assert (await client.get(URL, headers=account.headers)).status_code == 403
    assert (await client.patch(URL, json=change)).status_code == 401
    assert hub.thresholds["gpu_temp"] == 85


@pytest.mark.parametrize("change", [
    {"thresholds": {"humidity": 50}},  # not a configured metric
    {"thresholds": {"gpu_temp": 0}},
    {"thresholds": {"gpu_temp": -5}},
    {"thresholds": {"gpu_temp": True}},
    {"thresholds": {"gpu_temp": "85"}},
    {"thresholds": {"gpu_temp": 1e16}},
    {"warning_percent": 49},
    {"warning_percent": 100},
    {"warning_percent": 90.5},
    {"language": "fr"},
    {"time_zone": "Mars/Olympus"},
    {"time_zone": "localtime"},  # a file in the zone directory, not a zone
    {"time_zone": "../../etc/passwd"},
])
async def test_invalid_settings_are_refused(client, accounts, hub, change):
    assert (await client.patch(URL, json=change, headers=accounts.admin.headers)).status_code == 422
    assert hub.thresholds == dict(get_settings().realtime_thresholds)


async def test_seeding_keeps_an_admins_change():
    settings = get_settings()
    async with SessionLocal() as session:
        await site_settings.ensure_defaults(session, settings)
        stored = await session.get(MetricThreshold, "gpu_temp")
        stored.threshold = 80
        await session.commit()
        await site_settings.ensure_defaults(session, settings)  # the migrate step runs on every start
        assert (await session.get(MetricThreshold, "gpu_temp")).threshold == 80
        assert len((await session.scalars(select(MetricThreshold))).all()) == len(settings.realtime_thresholds)
        assert (await session.get(SystemSettings, site_settings.SETTINGS_ID)).warning_percent == 90


async def test_the_hub_reads_stored_settings():
    async with SessionLocal() as session:
        session.add(SystemSettings(id=site_settings.SETTINGS_ID, warning_percent=70, language="zh",
                                   time_zone="Asia/Kuala_Lumpur"))
        session.add(MetricThreshold(category="cpu_temp", threshold=80))
        await session.commit()
    hub = RealtimeHub(get_settings(), seed=1)
    await hub.sync_settings()  # what a starting API, or another instance's change, goes through
    assert hub.thresholds["cpu_temp"] == 80 and hub.thresholds["gpu_temp"] == 85 and hub.warning_percent == 70


def test_fans_alert_when_slow(monkeypatch):
    monkeypatch.setattr(realtime_service, "SPIKE_PROBABILITY", 0.2)
    hub = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 5}), seed=4)
    summary = hub.tick()["summary"]["fan_rpm"]
    fans = [r for r in hub.last_readings if r["category"] == "fan_rpm"]
    assert any(r["is_anomaly"] for r in fans)
    assert all(r["is_anomaly"] == (r["value"] < 3000) for r in fans)
    slowest = min(fans, key=lambda r: r["value"])
    assert summary["alert_below"] and summary["worst"] == slowest["value"] == summary["minimum"]
    assert (summary["worst_server"], summary["worst_sensor"]) == (slowest["server"], slowest["sensor"])
    gpu = hub.tick()["summary"]["gpu_temp"]
    assert not gpu["alert_below"] and gpu["worst"] == gpu["maximum"]


def test_load_is_the_share_of_the_limit_on_the_alerting_side():
    assert load(85, 85, below=False) == 100 and load(42.5, 85, below=False) == 50
    assert load(3000, 3000, below=True) == 100 and load(6000, 3000, below=True) == 50 and load(1500, 3000, below=True) == 200
    assert load(0, 3000, below=True) == float("inf")


def test_simulated_fans_run_well_above_their_limit():
    rng = random.Random(3)
    values = [generate_value("fan_rpm", 3000, rng, below=True) for _ in range(50_000)]
    assert 10_600 < sum(values) / len(values) < 11_600
    assert 0.0001 < sum(v < 3000 for v in values) / len(values) < 0.0006


@pytest.mark.usefixtures("hub")
async def test_raising_a_limit_means_fewer_alerts_not_moved_readings(monkeypatch):
    monkeypatch.setattr(realtime_service, "SPIKE_PROBABILITY", 0.05)
    low = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 20}), seed=9)
    high = RealtimeHub(get_settings().model_copy(update={"realtime_servers": 20}), seed=9)
    high.apply_settings({"gpu_temp": 95}, 90)
    low.tick()
    high.tick()
    gpu = lambda h: [r for r in h.last_readings if r["category"] == "gpu_temp"]  # noqa: E731
    assert [r["value"] for r in gpu(low)] == [r["value"] for r in gpu(high)]  # same readings
    assert sum(r["is_anomaly"] for r in gpu(high)) < sum(r["is_anomaly"] for r in gpu(low))


def test_a_poll_that_read_before_a_save_does_not_undo_it():
    from datetime import datetime

    hub = RealtimeHub(get_settings(), seed=1)
    hub.apply_settings({"gpu_temp": 80}, 85, datetime(2026, 10, 13, 6, 0, 1))  # the admin's save
    hub.apply_settings({"gpu_temp": 85}, 90, datetime(2026, 10, 13, 6, 0, 0))  # a poll that read just before it
    assert hub.thresholds["gpu_temp"] == 80 and hub.warning_percent == 85
    hub.apply_settings({"gpu_temp": 85}, 90)  # a poll that read before the first save created the row
    assert hub.thresholds["gpu_temp"] == 80
    hub.apply_settings({"gpu_temp": 82}, 85, datetime(2026, 10, 13, 6, 0, 2))  # a later change applies
    assert hub.thresholds["gpu_temp"] == 82
