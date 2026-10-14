from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, SessionDep
from app.core.security import create_access_token
from app.schemas.auth import RegisterRequest, TokenResponse, UserOut
from app.services import users

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, session: SessionDep) -> UserOut:
    """Create an account with the `viewer` role. Admins assign other roles."""
    return UserOut.model_validate(await users.create_user(session, data))


@router.post("/login", response_model=TokenResponse)
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep) -> TokenResponse:
    """OAuth2 password flow (form fields `username`, `password`); returns a bearer JWT."""
    user = await users.authenticate(session, form.username, form.password)
    token, expires_in = create_access_token(user.id, user.token_version)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user: CurrentUser, session: SessionDep) -> None:
    """End every session of this user: all tokens issued so far stop working, on every device."""
    await users.logout_everywhere(session, user)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    """The signed-in user, with the role read from the database."""
    return UserOut.model_validate(user)
