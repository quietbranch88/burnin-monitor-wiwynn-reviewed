"""Create the demo accounts, sample records and first settings. Safe to run on every start."""

import asyncio
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models import Record, Role, User
from app.schemas.auth import RegisterRequest
from app.services import importer, site_settings, users

logger = logging.getLogger("app.seed")

DEMO_ACCOUNTS = (
    ("admin", "admin@example.com", Role.admin, "seed_admin_password"),
    ("engineer", "engineer@example.com", Role.user, "seed_user_password"),
    ("viewer", "viewer@example.com", Role.viewer, "seed_viewer_password"),
)


async def seed() -> None:
    settings = get_settings()
    async with SessionLocal() as session:
        accounts: dict[str, User] = {}
        for username, email, role, password_setting in DEMO_ACCOUNTS:
            user = await session.scalar(select(User).where(User.username == username))
            if user is None:
                try:
                    data = RegisterRequest(username=username, email=email, password=getattr(settings, password_setting))
                except ValidationError as exc:
                    reasons = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
                    raise SystemExit(f"{password_setting.upper()} is not accepted: {reasons}") from None
                user = await users.create_user(session, data, role=role)
                logger.info("Created demo account %s (%s)", username, role.value)
            accounts[username] = user

        sample = Path(settings.seed_sample_csv)
        has_records = await session.scalar(select(func.count()).select_from(Record))
        if not has_records and sample.is_file():
            rows = importer.parse_records(sample.read_bytes(), sample.name, "text/csv")
            # Split ownership so the permission rules are visible in the demo:
            # the engineer can edit their own rows but not the admin's.
            half = len(rows) // 2
            importer.stage_records(session, accounts["engineer"], rows[:half])
            importer.stage_records(session, accounts["admin"], rows[half:])
            await session.commit()  # one transaction: all samples or none
            logger.info("Imported %d sample records from %s", len(rows), sample)
        await site_settings.ensure_defaults(session, settings)
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(seed())
