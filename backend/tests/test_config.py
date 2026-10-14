"""Every setting: where it is documented, that a bad value stops the start, and that it is used."""

import re
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from app.core.config import Settings, get_settings, load_settings
from app.core.security import create_access_token, hash_password
from app.db.session import engine
from app.main import create_app
from app.services.realtime import RealtimeHub
from app.services.retention import RetentionJob
from app.services.system_logs import build_log_writer
from pydantic import ValidationError

REPO = Path("/repo")  # the test image copies .env.example and docker-compose.yml here
FIELDS = {name.upper() for name in Settings.model_fields}
# Read by Compose or the frontend, not by the API's Settings.
NOT_API = {"MARIADB_ROOT_PASSWORD", "MARIADB_DATABASE", "MARIADB_USER", "MARIADB_PASSWORD",
           "API_PORT", "FRONTEND_PORT"}
# Fixed for a deployment: set only by the tests (or left at the default), not offered in .env.
INTERNAL = {"APP_NAME", "DB_ECHO", "JWT_ALGORITHM", "BCRYPT_ROUNDS", "REALTIME_ENABLED", "REALTIME_RECENT_SIZE",
            "RETENTION_ENABLED", "LOG_FLUSH_INTERVAL_SECONDS", "LOG_FLUSH_BATCH_SIZE", "SEED_SAMPLE_CSV"}
SECRET = "x" * 32


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    """Compare with the code defaults, not the test runner's environment (LOG_LEVEL, BCRYPT_ROUNDS...)."""
    for name in FIELDS:
        monkeypatch.delenv(name, raising=False)


def env_example() -> dict[str, str]:
    lines = (REPO / ".env.example").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def compose_api_env() -> dict[str, str]:
    """The `x-api-env` and `x-seed-env` blocks: NAME -> the default after `:-`, or the raw value."""
    text = (REPO / "docker-compose.yml").read_text()
    pairs: dict[str, str] = {}
    for anchor in ("x-api-env: &api_env\n", "x-seed-env: &seed_env\n"):
        block = text.split(anchor, 1)[1].split("\n\n", 1)[0]
        pairs |= dict(re.findall(r"^  ([A-Z_]+): (.+)$", block, re.M))
    return {name: (m.group(1) if (m := re.fullmatch(r"\$\{\w+:-(.*)\}", value)) else value) for name, value in pairs.items()}


def test_every_setting_is_offered_in_env_example_and_compose_or_is_internal():
    example, compose = set(env_example()), set(compose_api_env())
    assert example - FIELDS - NOT_API == set()  # a misspelt name would be ignored silently
    assert compose - FIELDS == set()
    assert FIELDS - compose == INTERNAL  # a new setting must be offered or listed as internal
    assert compose - {"DATABASE_URL"} <= example  # every offered setting is documented


def test_env_example_compose_and_code_agree_on_every_default():
    example, compose = env_example(), compose_api_env()
    offered = {name: value for name, value in compose.items() if name not in {"DATABASE_URL", "JWT_SECRET"}}
    assert {name: example[name] for name in offered} == offered
    from_example = Settings(jwt_secret=SECRET, **{name.lower(): value for name, value in offered.items()})
    defaults = Settings(jwt_secret=SECRET)
    for name in offered:
        assert getattr(from_example, name.lower()) == getattr(defaults, name.lower()), name


@pytest.mark.parametrize(("field", "value"), [
    ("jwt_secret", "short"),
    ("jwt_expire_minutes", 0), ("jwt_expire_minutes", 1441),
    ("bcrypt_rounds", 3), ("bcrypt_rounds", 16),
    ("db_pool_size", 0), ("db_pool_size", 101),
    ("db_max_overflow", -1), ("db_max_overflow", 101),
    ("db_pool_timeout", 0),
    ("db_pool_recycle", 59),
    ("realtime_interval_seconds", 0),
    ("realtime_thresholds", "cpu_temp:0"), ("realtime_thresholds", "cpu_temp:inf"), ("realtime_thresholds", "cpu_temp"),
    ("realtime_alert_below", "fan_rmp"),  # set, but not a metric
    ("realtime_warning_percent", 49), ("realtime_warning_percent", 100),
    ("display_language", "fr"),
    ("display_tz", "Mars/Olympus"), ("display_tz", "localtime"), ("display_tz", "../../etc/passwd"),
    ("realtime_servers", 0), ("realtime_servers", 2001),
    ("realtime_flush_interval_seconds", 0),
    ("realtime_flush_batch_size", 0),
    ("realtime_recent_size", 9),
    ("realtime_max_pending", 99),
    ("realtime_write_concurrency", 0), ("realtime_write_concurrency", 9),
    ("realtime_retention_hours", 0),
    ("log_retention_days", 0),
    ("log_flush_interval_seconds", 0),
    ("log_flush_batch_size", 0),
    ("log_level", "garbage"),
])
def test_a_value_out_of_range_stops_the_start(field, value):
    with pytest.raises(ValidationError):
        Settings(**{"jwt_secret": SECRET, field: value})


def test_the_generator_writers_and_purge_use_their_settings():
    settings = Settings(
        jwt_secret=SECRET, realtime_interval_seconds=2, realtime_servers=3,
        realtime_thresholds="cpu_temp:80,fan_rpm:2500", realtime_alert_below="fan_rpm", realtime_warning_percent=75,
        realtime_recent_size=20, realtime_flush_batch_size=7, realtime_flush_interval_seconds=3,
        realtime_max_pending=200, realtime_write_concurrency=2, realtime_retention_hours=0.5, log_retention_days=2,
        log_flush_interval_seconds=4, log_flush_batch_size=9,
    )
    hub = RealtimeHub(settings, seed=1)
    assert (hub.interval, hub.servers, hub.thresholds, hub.below, hub.warning_percent) == (
        2, ["S0001", "S0002", "S0003"], {"cpu_temp": 80, "fan_rpm": 2500}, {"fan_rpm"}, 75)
    assert hub.recent.maxlen == 20
    assert (hub.writer.batch_size, hub.writer.interval, hub.writer._buffer.maxlen, hub.writer._concurrency._value) == (7, 3, 200, 2)
    assert hub.minute_writer.interval == 3
    assert hub.readings_per_second == 3 * (2 + 10) / 2  # 2 CPUs and 10 fans per server, every 2 s
    retention = RetentionJob(settings)
    assert (retention.readings_hours, retention.log_days) == (0.5, 2)
    logs = build_log_writer(settings)
    assert (logs.batch_size, logs.interval) == (9, 4)


def test_a_bad_setting_stops_the_start_without_printing_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(SystemExit) as stop:
        load_settings()
    assert "JWT_SECRET" in str(stop.value) and "too-short" not in str(stop.value)
    # The cross-field check reports the whole input; the message must still name only the setting.
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("REALTIME_ALERT_BELOW", "fan_rmp")
    monkeypatch.setenv("SEED_VIEWER_PASSWORD", "Canary-Pass-2026")
    with pytest.raises(SystemExit) as stop:
        load_settings()
    assert "REALTIME_ALERT_BELOW" in str(stop.value) and "Canary-Pass-2026" not in str(stop.value)


def test_the_engine_tokens_and_hashes_use_their_settings():
    settings = get_settings()  # cached at import, from the runner's environment
    pool = engine.sync_engine.pool
    assert (pool.size(), pool._max_overflow, pool._timeout, pool._recycle) == (
        settings.db_pool_size, settings.db_max_overflow, settings.db_pool_timeout, settings.db_pool_recycle)
    assert engine.sync_engine.echo is settings.db_echo
    token, lifetime = create_access_token(1, 0)
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    # `iat` keeps sub-second precision; PyJWT writes `exp` in whole seconds.
    assert lifetime == settings.jwt_expire_minutes * 60 == claims["exp"] - int(claims["iat"])
    assert claims["exp"] > datetime.now(UTC).timestamp()
    assert hash_password("Pass2026x").startswith(f"$2b${settings.bcrypt_rounds:02d}$")
    assert create_app().title == settings.app_name
