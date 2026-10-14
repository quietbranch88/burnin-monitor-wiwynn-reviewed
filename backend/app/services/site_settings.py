"""Settings an admin changes at runtime: alert thresholds, the warning level, the interface language
and the plant's time zone.

.env gives the first values; the migrate step stores them once, and from then on the
database is the source. Only categories configured in .env exist: an admin changes their
thresholds but cannot add or remove metrics.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, ValidationFailedError
from app.core.time import utcnow
from app.db.session import DUPLICATE_KEY, error_code
from app.models import MetricThreshold, SystemSettings, User
from app.schemas.settings import SettingsUpdate

logger = logging.getLogger("app.settings")

SETTINGS_ID = 1
Language = Literal["zh", "en"]


@dataclass(frozen=True)
class CurrentSettings:
    warning_percent: int
    language: Language
    time_zone: str
    thresholds: dict[str, float]
    updated_at: datetime | None
    updated_by: str | None


async def load(session: AsyncSession, settings: Settings) -> CurrentSettings:
    row = await session.get(SystemSettings, SETTINGS_ID)
    stored = {t.category: t.threshold for t in await session.scalars(select(MetricThreshold))}
    thresholds = {category: stored.get(category, value) for category, value in settings.realtime_thresholds.items()}
    if row is None:
        return CurrentSettings(settings.realtime_warning_percent, settings.display_language, settings.display_tz,
                               thresholds, None, None)
    editor = await session.get(User, row.updated_by) if row.updated_by is not None else None
    # The column holds what SettingsUpdate validated or .env gave: one of the two languages.
    return CurrentSettings(row.warning_percent, cast(Language, row.language), row.time_zone, thresholds, row.updated_at,
                           editor.username if editor else None)


async def ensure_defaults(session: AsyncSession, settings: Settings) -> None:
    """Store the .env values for whatever is not stored yet; never overwrite an admin's change."""
    if await session.get(SystemSettings, SETTINGS_ID) is None:
        session.add(SystemSettings(id=SETTINGS_ID, warning_percent=settings.realtime_warning_percent,
                                   language=settings.display_language, time_zone=settings.display_tz))
    stored = set(await session.scalars(select(MetricThreshold.category)))
    session.add_all(
        MetricThreshold(category=category, threshold=value)
        for category, value in settings.realtime_thresholds.items()
        if category not in stored
    )
    await session.commit()


async def update(session: AsyncSession, settings: Settings, actor: User, data: SettingsUpdate) -> CurrentSettings:
    unknown = sorted(set(data.thresholds or {}) - set(settings.realtime_thresholds))
    if unknown:
        raise ValidationFailedError(
            f"Unknown metric: {', '.join(unknown)}",
            errors=[{"field": f"thresholds.{name}", "message": "Unknown metric"} for name in unknown],
        )
    # Locking the settings row makes two admins saving at once apply one after the other.
    row = await session.get(SystemSettings, SETTINGS_ID, with_for_update=True)
    if row is None:
        row = SystemSettings(id=SETTINGS_ID, warning_percent=settings.realtime_warning_percent,
                             language=settings.display_language, time_zone=settings.display_tz)
        session.add(row)
    changes = []
    if data.warning_percent is not None and data.warning_percent != row.warning_percent:
        changes.append(f"warning {row.warning_percent}% -> {data.warning_percent}%")
        row.warning_percent = data.warning_percent
    if data.language is not None and data.language != row.language:
        changes.append(f"language {row.language} -> {data.language}")
        row.language = data.language
    if data.time_zone is not None and data.time_zone != row.time_zone:
        changes.append(f"time zone {row.time_zone} -> {data.time_zone}")
        row.time_zone = data.time_zone
    for category, value in (data.thresholds or {}).items():
        stored = await session.get(MetricThreshold, category)
        old = stored.threshold if stored else settings.realtime_thresholds[category]
        if value == old:
            continue
        changes.append(f"{category} threshold {old:g} -> {value:g}")
        if stored is None:
            session.add(MetricThreshold(category=category, threshold=value, updated_by=actor.id))
        else:
            stored.threshold, stored.updated_by = value, actor.id
    if changes:
        row.updated_at, row.updated_by = utcnow(), actor.id
    try:
        await session.commit()
    except IntegrityError as exc:
        # Only without the seeded row: two first saves at once both insert it.
        if error_code(exc) != DUPLICATE_KEY:
            raise
        raise ConflictError("The settings were saved by someone else at the same moment; try again") from None
    if changes:
        logger.info("Admin %s changed settings: %s", actor.username, ", ".join(changes), extra={"user_id": actor.id})
    return await load(session, settings)
