import logging

from sqlalchemy import func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationFailedError
from app.core.security import hash_password_async, verify_password_async
from app.models.user import Role, User
from app.schemas.admin import UserAdminUpdate
from app.schemas.auth import RegisterRequest

logger = logging.getLogger("app.users")


async def create_user(session: AsyncSession, data: RegisterRequest, role: Role = Role.viewer, actor: User | None = None) -> User:
    """Self-registration grants Viewer (least privilege); an admin may create any role (`actor`)."""
    # Hash first: bcrypt runs in a thread for about 250 ms, and the query below would otherwise
    # hold a pooled connection for all of it (see authenticate).
    hashed = await hash_password_async(data.password)
    taken = await session.scalar(
        select(User.id).where(or_(User.username == data.username, User.email == data.email)).limit(1)
    )
    if taken is not None:
        raise ConflictError("Username or email is already registered")
    user = User(username=data.username, email=data.email, hashed_password=hashed, role=role)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:  # concurrent registration with the same name
        await session.rollback()
        raise ConflictError("Username or email is already registered") from exc
    if actor is None:
        logger.info("User registered: %s (%s)", user.username, user.role.value, extra={"user_id": user.id})
    else:
        logger.info("Admin %s created %s (%s)", actor.username, user.username, user.role.value, extra={"user_id": actor.id})
    return user


async def reset_password(session: AsyncSession, actor: User, user_id: int, password: str) -> None:
    """Set a new password and end every session of that user, as a logout would."""
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    user.hashed_password = await hash_password_async(password)
    user.token_version += 1
    await session.commit()
    logger.info("Admin %s reset the password of %s", actor.username, user.username, extra={"user_id": actor.id})


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    # bcrypt takes about 250 ms in a thread. End the transaction first so the pooled connection
    # is free meanwhile: otherwise a burst of logins holds every connection, and unrelated
    # requests wait for the pool and answer 503. The user is detached so the rollback does not
    # expire its loaded columns; nothing below writes it.
    if user is not None:
        session.expunge(user)
    await session.rollback()
    valid = await verify_password_async(password, user.hashed_password if user else None)
    if user is None or not valid:
        # The typed username is not logged: people sometimes type a password into that field.
        if user is None:
            logger.warning("Login failed: unknown username")
        else:
            logger.warning("Login failed: wrong password for %s", user.username, extra={"user_id": user.id})
        raise AuthenticationError("Incorrect username or password")
    if not user.is_active:
        # Same answer as a wrong password, so the response never confirms a password; the
        # real reason is in the system log for admins.
        logger.warning("Login failed: account disabled for %s", user.username, extra={"user_id": user.id})
        raise AuthenticationError("Incorrect username or password")
    logger.info("Login succeeded: %s", user.username, extra={"user_id": user.id})
    return user


async def logout_everywhere(session: AsyncSession, user: User) -> None:
    user.token_version += 1
    await session.commit()
    logger.info("Logout: %s", user.username, extra={"user_id": user.id})


async def list_users(session: AsyncSession, page: int, page_size: int, q: str | None = None) -> tuple[list[User], int]:
    conditions = []
    if q:
        conditions.append(or_(User.username.contains(q, autoescape=True), User.email.contains(q, autoescape=True)))
    total = await session.scalar(select(func.count()).select_from(User).where(*conditions))
    query = select(User).where(*conditions).order_by(User.id).offset((page - 1) * page_size).limit(page_size)
    return list(await session.scalars(query)), total or 0


async def update_user(session: AsyncSession, actor: User, user_id: int, data: UserAdminUpdate) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    demotes = (data.role is not None and data.role != Role.admin) or data.is_active is False
    if user.id == actor.id and demotes:
        raise ValidationFailedError("Admins cannot demote or deactivate themselves")
    if user.role == Role.admin and user.is_active and demotes:
        # Lock every active admin row, so two admins demoting each other at once cannot both pass.
        admins = (await session.scalars(
            select(User).where(User.role == Role.admin, User.is_active == true()).with_for_update()
        )).all()
        if len(admins) <= 1:
            raise ValidationFailedError("At least one active admin must remain")
    changes = []
    if data.role is not None and data.role != user.role:
        changes.append(f"role {user.role.value} -> {data.role.value}")
        user.role = data.role
    if data.is_active is not None and data.is_active != user.is_active:
        changes.append(f"active {user.is_active} -> {data.is_active}")
        user.is_active = data.is_active
        if not data.is_active:
            user.token_version += 1  # reactivating later must not revive the old tokens
    await session.commit()
    if changes:
        logger.info("Admin %s changed %s: %s", actor.username, user.username, ", ".join(changes), extra={"user_id": actor.id})
    return user
