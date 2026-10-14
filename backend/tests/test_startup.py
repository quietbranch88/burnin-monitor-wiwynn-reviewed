from app.core.config import Settings
from app.db.session import SessionLocal
from app.models import Record, Role, User
from app.seed import seed
from sqlalchemy import func, select


async def test_seed_is_safe_to_rerun():
    await seed()
    await seed()  # every `docker compose up` runs it again
    async with SessionLocal() as session:
        users = {u.username: u for u in await session.scalars(select(User))}
        owners = dict((await session.execute(select(Record.owner_id, func.count()).group_by(Record.owner_id))).all())
    assert {name: u.role for name, u in users.items()} == {"admin": Role.admin, "engineer": Role.user, "viewer": Role.viewer}
    # 112 samples imported once, split so the engineer owns half and the admin the rest.
    assert owners == {users["engineer"].id: 56, users["admin"].id: 56}


def test_thresholds_are_parsed_with_spaces():
    settings = Settings(jwt_secret="x" * 32, realtime_thresholds="cpu_temp:90, power_w:2700")
    assert settings.realtime_thresholds == {"cpu_temp": 90.0, "power_w": 2700.0}
