from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.dependencies.models import Dependant  # internal: route.dependant lists the query names; pinned FastAPI
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.core.errors import AuthenticationError, PermissionDeniedError, ValidationFailedError
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import Role, User
from app.services.realtime import RealtimeHub

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _query_names(dependant: Dependant) -> set[str]:
    """The route's query parameters, including those of its dependencies (e.g. paging)."""
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_names(sub)
    return names


async def reject_unknown_query(connection: HTTPConnection) -> None:
    """A misspelt filter such as `?catgory=` would otherwise be ignored and return everything."""
    route = connection.scope.get("route")
    if connection.scope["type"] != "http" or route is None:
        return
    known = _query_names(route.dependant)
    unknown = sorted(set(connection.query_params) - known)
    if unknown:
        raise ValidationFailedError(
            "Request validation failed",
            errors=[{"field": f"query.{name}", "message": "Unknown query parameter"} for name in unknown],
        )


async def load_active_user(session: AsyncSession, token: str) -> User:
    user_id, version = decode_access_token(token)
    user = await session.get(User, user_id)
    # A logout increments the version, which ends every token issued before it.
    if user is None or not user.is_active or user.token_version != version:
        raise AuthenticationError("Invalid or expired token")
    return user


async def get_current_user(request: Request, session: SessionDep, token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    # The role is read from the database, so a role change applies to the next request.
    user = await load_active_user(session, token)
    request.state.user_id = user.id
    # Checked after the login, so a caller without a token gets 401, not a list of accepted names.
    await reject_unknown_query(request)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role):
    async def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise PermissionDeniedError("Your role does not allow this action")
        return user

    return dependency


EditorUser = Annotated[User, Depends(require_roles(Role.admin, Role.user))]
AdminUser = Annotated[User, Depends(require_roles(Role.admin))]


def get_hub(request: Request) -> RealtimeHub:
    return request.app.state.hub


HubDep = Annotated[RealtimeHub, Depends(get_hub)]


class Pagination:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> None:
        self.page = page
        self.page_size = page_size


PageDep = Annotated[Pagination, Depends()]
