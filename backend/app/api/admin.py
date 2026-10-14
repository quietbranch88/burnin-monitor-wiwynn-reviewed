from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response, status

from app.api.deps import AdminUser, HubDep, PageDep, SessionDep
from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.core.time import UTCDateTime
from app.schemas.admin import DatabaseStatus, PasswordReset, SystemLogOut, UserAdminCreate, UserAdminUpdate
from app.schemas.auth import RegisterRequest, UserOut
from app.schemas.common import ErrorResponse, Page
from app.schemas.realtime import HistoryPage, PersistedReadingOut
from app.schemas.settings import SettingsOut, SettingsUpdate, ThresholdOut
from app.services import admin, site_settings, users
from app.services.admin import LogFilters
from app.services.realtime import RealtimeHub

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=Page[UserOut])
async def list_users(
    session: SessionDep,
    _: AdminUser,
    paging: PageDep,
    q: Annotated[str | None, Query(max_length=100, description="Username or email contains")] = None,
) -> Page[UserOut]:
    """Admin only. Paged; `q` matches part of a username or email."""
    items, total = await users.list_users(session, paging.page, paging.page_size, q)
    return Page.build([UserOut.model_validate(u) for u in items], total, paging.page, paging.page_size)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             responses={409: {"model": ErrorResponse, "description": "Username or email is already registered"}})
async def create_user(data: UserAdminCreate, session: SessionDep, actor: AdminUser) -> UserOut:
    """Admin only. Create an account with any role; self-registration always gives Viewer."""
    user = await users.create_user(session, RegisterRequest(**data.model_dump(exclude={"role"})), role=data.role, actor=actor)
    return UserOut.model_validate(user)


@router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT,
            responses={404: {"model": ErrorResponse, "description": "No such user"}})
async def reset_password(user_id: int, data: PasswordReset, session: SessionDep, actor: AdminUser) -> Response:
    """Admin only. Set a new password; every session of that user ends, as after a logout."""
    await users.reset_password(session, actor, user_id, data.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, data: UserAdminUpdate, session: SessionDep, actor: AdminUser) -> UserOut:
    """Change role and/or active flag. Admins cannot demote or deactivate themselves."""
    return UserOut.model_validate(await users.update_user(session, actor, user_id, data))


@router.get("/logs", response_model=Page[SystemLogOut])
async def list_logs(
    session: SessionDep,
    _: AdminUser,
    paging: PageDep,
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
    path: Annotated[str | None, Query(max_length=200)] = None,
    status_code: Annotated[int | None, Query(ge=100, le=599)] = None,
    user_id: int | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Message contains")] = None,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
    logger: Annotated[str | None, Query(max_length=100, description="Logger name or prefix, e.g. app.users")] = None,
    request_id: Annotated[str | None, Query(max_length=36, description="Every log line of one request")] = None,
    kind: Annotated[Literal["events", "requests"] | None, Query(
        description="`events`: changes, logins and other events; `requests`: one line per API request")] = None,
) -> Page[SystemLogOut]:
    """Admin only. Newest first; every filter is optional and they combine. `user_id` is who did it."""
    filters = LogFilters(level, path, status_code, user_id, q, start, end, logger, request_id, kind)
    items, total = await admin.list_logs(session, filters, paging.page, paging.page_size)
    names = await admin.usernames(session, {i.user_id for i in items if i.user_id is not None})
    out = [SystemLogOut.model_validate(i).model_copy(update={"username": names.get(i.user_id) if i.user_id is not None else None})
           for i in items]
    return Page.build(out, total, paging.page, paging.page_size)


@router.get("/db-status", response_model=DatabaseStatus)
async def db_status(request: Request, session: SessionDep, hub: HubDep, _: AdminUser) -> DatabaseStatus:
    """Admin only. Connection, pool, approximate table sizes and the batch writers' counters."""
    return await admin.database_status(session, get_settings(), hub, request.app.state.log_writer)


@router.get("/realtime/history", response_model=HistoryPage)
async def realtime_history(
    session: SessionDep,
    _: AdminUser,
    category: Annotated[str | None, Query(max_length=50)] = None,
    server: Annotated[str | None, Query(max_length=20)] = None,
    sensor: Annotated[str | None, Query(max_length=20)] = None,
    anomalies_only: bool = False,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
    before_at: Annotated[UTCDateTime | None, Query(description="With before_id: the previous page's next_before_at")] = None,
    before_id: Annotated[int | None, Query(ge=1, description="With before_at: the previous page's next_before_id")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> HistoryPage:
    """Persisted readings, newest generation time first. Readings still in the write buffer appear after the next flush."""
    if (before_at is None) != (before_id is None):
        raise ValidationFailedError("before_at and before_id go together",
                                    errors=[{"field": "query.before_id", "message": "Pass both values from the previous page"}])
    items, cursor = await admin.list_history(
        session,
        category=category,
        server=server,
        sensor=sensor,
        anomalies_only=anomalies_only,
        start=start,
        end=end,
        before_at=before_at,
        before_id=before_id,
        limit=limit,
    )
    return HistoryPage(items=[PersistedReadingOut.model_validate(i) for i in items],
                       next_before_at=cursor[0] if cursor else None, next_before_id=cursor[1] if cursor else None)


def _settings_out(current: site_settings.CurrentSettings, hub: RealtimeHub) -> SettingsOut:
    defaults = get_settings().realtime_thresholds
    return SettingsOut(
        warning_percent=current.warning_percent,
        language=current.language,
        time_zone=current.time_zone,
        thresholds=[
            ThresholdOut(category=c, threshold=v, default=defaults[c], alert_below=c in hub.below)
            for c, v in current.thresholds.items()
        ],
        updated_at=current.updated_at,
        updated_by=current.updated_by,
    )


@router.get("/settings", response_model=SettingsOut)
async def read_settings(session: SessionDep, hub: HubDep, _: AdminUser) -> SettingsOut:
    """Alert thresholds, the warning level and the interface language."""
    return _settings_out(await site_settings.load(session, get_settings()), hub)


@router.patch("/settings", response_model=SettingsOut)
async def change_settings(data: SettingsUpdate, session: SessionDep, hub: HubDep, actor: AdminUser) -> SettingsOut:
    """Change any of them; the generator uses new thresholds from the next second. Each change is logged."""
    current = await site_settings.update(session, get_settings(), actor, data)
    hub.apply_settings(current.thresholds, current.warning_percent, current.updated_at)
    return _settings_out(current, hub)
