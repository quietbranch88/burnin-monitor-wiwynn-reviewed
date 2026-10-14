from typing import Any

from fastapi import APIRouter, Depends

from app.api import admin, analytics, auth, realtime, records, settings
from app.api.deps import reject_unknown_query
from app.schemas.common import ErrorResponse


def _error(description: str, **extra: object) -> dict:
    return {"model": ErrorResponse, "description": description, **extra}


RETRY_AFTER = {"Retry-After": {"description": "Seconds to wait before retrying", "schema": {"type": "integer"}}}
# Documented once for every route that needs a login; FastAPI lists only 200 and 422 by itself.
SIGNED_IN: dict[int | str, dict[str, Any]] = {
    401: _error("Missing, expired or ended token", headers={"WWW-Authenticate": {"schema": {"type": "string"}}}),
    403: _error("The role may not do this, or the record belongs to someone else"),
    503: _error("Database unavailable; retry later", headers=RETRY_AFTER),
}

api_router = APIRouter(prefix="/api/v1")
# Routes that need a login check the query names inside get_current_user, after the token.
for module in (auth, settings):
    api_router.include_router(
        module.router, responses={503: SIGNED_IN[503]}, dependencies=[Depends(reject_unknown_query)]
    )
for module in (records, realtime, analytics, admin):
    api_router.include_router(module.router, responses=SIGNED_IN)
