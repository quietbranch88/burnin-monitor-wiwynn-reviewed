import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.time import to_storage
from app.models.record import Record
from app.models.user import Role, User
from app.schemas.record import RecordCreate, RecordOut, RecordSort, RecordUpdate, SortOrder
from app.services import idempotency

logger = logging.getLogger("app.records")

SORT_COLUMNS = {
    RecordSort.recorded_at: Record.recorded_at,
    RecordSort.value: Record.value,
    RecordSort.title: Record.title,
    RecordSort.category: Record.category,
    RecordSort.created_at: Record.created_at,
    RecordSort.id: Record.id,
}


@dataclass(frozen=True)
class RecordFilters:
    category: str | None = None
    q: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    start: datetime | None = None
    end: datetime | None = None
    owner_id: int | None = None
    server: str | None = None

    def apply(self, query: Select) -> Select:
        if self.category:
            query = query.where(Record.category == self.category.strip().lower())  # stored lower-case
        if self.q:
            query = query.where(Record.title.contains(self.q, autoescape=True))
        if self.min_value is not None:
            query = query.where(Record.value >= self.min_value)
        if self.max_value is not None:
            query = query.where(Record.value <= self.max_value)
        if self.start is not None:
            query = query.where(Record.recorded_at >= to_storage(self.start))
        if self.end is not None:
            query = query.where(Record.recorded_at <= to_storage(self.end))
        if self.owner_id is not None:
            query = query.where(Record.owner_id == self.owner_id)
        if self.server:
            query = query.where(Record.server == self.server)
        return query


def can_modify(user: User, record: Record) -> bool:
    return user.role == Role.admin or (user.role == Role.user and record.owner_id == user.id)


async def list_records(
    session: AsyncSession,
    filters: RecordFilters,
    *,
    page: int,
    page_size: int,
    sort: RecordSort,
    order: SortOrder,
) -> tuple[list[Record], int]:
    total = await session.scalar(filters.apply(select(func.count()).select_from(Record)))
    column = SORT_COLUMNS[sort]
    direction = column.asc() if order == SortOrder.asc else column.desc()
    tiebreak = Record.id.asc() if order == SortOrder.asc else Record.id.desc()
    query = filters.apply(select(Record)).order_by(direction, tiebreak).offset((page - 1) * page_size).limit(page_size)
    return list(await session.scalars(query)), total or 0


async def list_categories(session: AsyncSession) -> list[str]:
    return list(await session.scalars(select(Record.category).distinct().order_by(Record.category)))


async def get_record(session: AsyncSession, record_id: int) -> Record:
    record = await session.get(Record, record_id)
    if record is None:
        raise NotFoundError("Record not found")
    return record


async def stage_record(session: AsyncSession, owner: User, data: RecordCreate) -> Record:
    """Add a record and load its owner, without committing."""
    record = Record(
        title=data.title,
        value=data.value,
        category=data.category,
        server=data.server,
        recorded_at=to_storage(data.recorded_at),
        owner_id=owner.id,
    )
    session.add(record)
    await session.flush()
    await session.refresh(record, attribute_names=["owner"])
    return record


async def create_record(session: AsyncSession, owner: User, data: RecordCreate) -> Record:
    record = await stage_record(session, owner, data)
    await session.commit()
    logger.info("Record %d created by %s", record.id, owner.username, extra={"user_id": owner.id})
    return record


async def create_record_once(session: AsyncSession, owner: User, data: RecordCreate, key: str) -> tuple[dict, bool]:
    """Create a record at most once per Idempotency-Key. Returns (response body, replayed)."""

    async def work() -> dict:
        return RecordOut.model_validate(await stage_record(session, owner, data)).model_dump(mode="json")

    request_hash = idempotency.fingerprint("create_record", data.model_dump(mode="json"))
    body, replayed = await idempotency.run_once(session, owner, key, request_hash, work)
    if not replayed:
        logger.info("Record %d created by %s", body["id"], owner.username, extra={"user_id": owner.id})
    return body, replayed


async def update_record(session: AsyncSession, actor: User, record_id: int, data: RecordUpdate) -> Record:
    record = await get_record(session, record_id)
    if not can_modify(actor, record):
        raise PermissionDeniedError("Only the creator or an admin can update this record")
    changes = data.model_dump(exclude_unset=True)  # the schema refuses null
    if "recorded_at" in changes:
        changes["recorded_at"] = to_storage(changes["recorded_at"])
    for field, value in changes.items():
        setattr(record, field, value)
    await session.commit()
    await session.refresh(record)
    if changes:
        logger.info("Record %d updated by %s: %s", record_id, actor.username, ", ".join(sorted(changes)),
                    extra={"user_id": actor.id})
    return record


async def delete_record(session: AsyncSession, actor: User, record_id: int) -> None:
    record = await get_record(session, record_id)
    if not can_modify(actor, record):
        raise PermissionDeniedError("Only the creator or an admin can delete this record")
    await session.delete(record)
    await session.commit()
    logger.info("Record %d deleted by %s", record_id, actor.username, extra={"user_id": actor.id})
