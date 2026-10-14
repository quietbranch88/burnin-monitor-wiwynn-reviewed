"""Fixtures run against a real MariaDB (the Compose `test-db` service)."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-thirty-two-characters")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("REALTIME_ENABLED", "false")
os.environ.setdefault("RETENTION_ENABLED", "false")

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal, engine
from app.main import create_app
from app.models import (
    GeneratorLease,
    IdempotencyKey,
    MetricThreshold,
    RealtimeAlert,
    RealtimeMinuteStat,
    RealtimeReading,
    Record,
    Role,
    SystemLog,
    SystemSettings,
    User,
)
from sqlalchemy import delete

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
async def migrated_database():
    """Run the real migrations down and up, so the schema under test is Alembic's."""
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["configure_logger"] = False
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")
    yield
    await engine.dispose()


@pytest.fixture(scope="session")
async def app():
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture(autouse=True)
async def clean_tables(app):
    # Write out log lines still buffered from the previous test, so they are deleted with the
    # table instead of arriving in the middle of this one.
    await app.state.log_writer.flush()
    async with SessionLocal() as session:
        # Settings first: they point at the admin who changed them.
        for model in (SystemSettings, MetricThreshold, IdempotencyKey, Record, RealtimeReading, RealtimeMinuteStat, SystemLog,
                      RealtimeAlert, GeneratorLease, User):
            await session.execute(delete(model))
        await session.commit()


@dataclass
class Account:
    user: User

    @property
    def headers(self) -> dict[str, str]:
        token, _ = create_access_token(self.user.id, self.user.token_version)
        return {"Authorization": f"Bearer {token}"}


@dataclass
class Accounts:
    admin: Account
    engineer: Account
    other: Account
    viewer: Account


@pytest.fixture
async def accounts() -> Accounts:
    created = {}
    async with SessionLocal() as session:
        for name, role in (("admin", Role.admin), ("engineer", Role.user), ("other", Role.user), ("viewer", Role.viewer)):
            password = f"{name}-password"
            user = User(username=name, email=f"{name}@example.com", hashed_password=hash_password(password), role=role)
            session.add(user)
            created[name] = user
        await session.commit()
    return Accounts(**{name: Account(user) for name, user in created.items()})


async def create_record(client: httpx.AsyncClient, account: Account, **overrides) -> dict:
    body = {"title": "Line 1 station 01 - CPU temperature", "value": 70.0, "category": "cpu_temp",
            "recorded_at": "2026-10-01T08:00:00Z", "server": "S0001"} | overrides
    response = await client.post("/api/v1/records", json=body, headers=account.headers)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def live_server(app):
    """Serve the app on a real socket in this event loop, for WebSocket tests."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/api/v1/ws/realtime"
    server.should_exit = True
    await task
