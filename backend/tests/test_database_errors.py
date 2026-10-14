"""Database failures are classified from real driver errors, not from their Python class."""

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db.session import engine, error_code, is_conflict, is_unavailable
from app.models import Base
from app.services import records
from sqlalchemy.ext.asyncio import create_async_engine


async def test_a_database_that_cannot_be_reached_counts_as_unavailable():
    unreachable = create_async_engine("mariadb+asyncmy://nobody:x@127.0.0.1:1/none", pool_pre_ping=True)
    with pytest.raises(sa.exc.OperationalError) as caught:
        async with unreachable.connect() as conn:
            await conn.execute(sa.select(sa.literal(1)))
    await unreachable.dispose()
    assert error_code(caught.value) == 2003 and is_unavailable(caught.value) and not is_conflict(caught.value)


async def test_a_sql_mistake_is_a_500_not_a_retryable_outage(client, accounts, monkeypatch):
    missing = sa.table("records", sa.column("no_such_column"))

    async def broken_query(session):
        await session.execute(sa.select(missing.c.no_such_column))

    monkeypatch.setattr(records, "list_categories", broken_query)
    response = await client.get("/api/v1/records/categories", headers=accounts.viewer.headers)
    # MariaDB 1054 also arrives as OperationalError; it is a bug, so no Retry-After.
    assert response.status_code == 500 and "Retry-After" not in response.headers


async def test_the_migration_matches_the_models():
    def diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn, opts={"compare_type": True}), Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
