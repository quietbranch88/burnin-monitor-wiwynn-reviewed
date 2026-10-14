from fastapi import APIRouter

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.schemas.settings import DisplaySettings
from app.services import site_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/display", response_model=DisplaySettings)
async def display(session: SessionDep) -> DisplaySettings:
    """The interface language and plant time zone an admin chose. Public: the login page needs them."""
    current = await site_settings.load(session, get_settings())
    return DisplaySettings(language=current.language, time_zone=current.time_zone)
