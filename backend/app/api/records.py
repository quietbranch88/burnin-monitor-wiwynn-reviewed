from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Response, UploadFile, status

from app.api.deps import CurrentUser, EditorUser, PageDep, SessionDep
from app.core.time import UTCDateTime
from app.schemas.common import ErrorResponse, Page
from app.schemas.record import MAX_ABS_VALUE, ImportResult, RecordCreate, RecordOut, RecordSort, RecordUpdate, SortOrder
from app.services import importer, records
from app.services.records import RecordFilters

router = APIRouter(prefix="/records", tags=["records"])

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=100,
        description="Optional. Send the same key again to retry safely: the stored response comes back "
        "with `Idempotent-Replayed: true`, and nothing is created twice. A key belongs to the user who sent it "
        "and is kept for 24 hours; the same key with a different body answers 409.",
    ),
]
REPLAYED_HEADER = "Idempotent-Replayed"


@router.get("", response_model=Page[RecordOut])
async def list_records(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    category: Annotated[str | None, Query(max_length=50)] = None,
    q: Annotated[str | None, Query(max_length=100, description="Title contains")] = None,
    min_value: Annotated[float | None, Query(allow_inf_nan=False, ge=-MAX_ABS_VALUE, le=MAX_ABS_VALUE)] = None,
    max_value: Annotated[float | None, Query(allow_inf_nan=False, ge=-MAX_ABS_VALUE, le=MAX_ABS_VALUE)] = None,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
    server: Annotated[str | None, Query(max_length=20, description="Server under test, e.g. S0017")] = None,
    mine: bool = False,
    sort: RecordSort = RecordSort.recorded_at,
    order: SortOrder = SortOrder.desc,
) -> Page[RecordOut]:
    """Any signed-in user. Filters combine; text filters match the whole value, in any case, except `q`, which searches titles."""
    filters = RecordFilters(category, q, min_value, max_value, start, end, user.id if mine else None, server)
    items, total = await records.list_records(
        session, filters, page=paging.page, page_size=paging.page_size, sort=sort, order=order
    )
    return Page.build([RecordOut.model_validate(r) for r in items], total, paging.page, paging.page_size)


@router.get("/categories", response_model=list[str])
async def list_categories(session: SessionDep, _: CurrentUser) -> list[str]:
    """Every category used by a record, sorted."""
    return await records.list_categories(session)


CREATED_HEADERS = {
    "Location": {"description": "Path of the new record", "schema": {"type": "string"}},
    REPLAYED_HEADER: {"description": "`true` when this is the stored answer to an earlier request", "schema": {"type": "string"}},
}


@router.post(
    "", response_model=RecordOut, status_code=status.HTTP_201_CREATED,
    responses={201: {"headers": CREATED_HEADERS}, 409: {"model": ErrorResponse, "description": "Idempotency-Key reused for a different body"}},
)
async def create_record(
    data: RecordCreate,
    session: SessionDep,
    user: EditorUser,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> RecordOut:
    """Engineer or admin. The record belongs to its creator; the answer has a `Location` header."""
    if idempotency_key is None:
        created = RecordOut.model_validate(await records.create_record(session, user, data))
    else:
        body, replayed = await records.create_record_once(session, user, data, idempotency_key)
        if replayed:
            response.headers[REPLAYED_HEADER] = "true"
        created = RecordOut.model_validate(body)
    response.headers["Location"] = f"/api/v1/records/{created.id}"
    return created


@router.post(
    "/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED,
    responses={
        201: {"headers": {REPLAYED_HEADER: CREATED_HEADERS[REPLAYED_HEADER]}},
        409: {"model": ErrorResponse, "description": "Idempotency-Key reused for a different file"},
        413: {"model": ErrorResponse, "description": "File over 2 MB"},
    },
)
async def import_records(
    session: SessionDep,
    user: EditorUser,
    response: Response,
    file: Annotated[UploadFile, File(description="CSV with title,value,category,recorded_at,server or a JSON list")],
    idempotency_key: IdempotencyKeyHeader = None,
) -> ImportResult:
    """Validate every row first; a single invalid row rejects the whole file (422 with row numbers)."""
    raw = await file.read(importer.MAX_BYTES + 1)
    imported, replayed = await importer.import_file(session, user, raw, file.filename, file.content_type, idempotency_key)
    if replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return ImportResult(imported=imported)


@router.get("/{record_id}", response_model=RecordOut, responses={404: {"model": ErrorResponse, "description": "No such record"}})
async def get_record(record_id: int, session: SessionDep, _: CurrentUser) -> RecordOut:
    return RecordOut.model_validate(await records.get_record(session, record_id))


@router.patch("/{record_id}", response_model=RecordOut, responses={404: {"model": ErrorResponse, "description": "No such record"}})
async def update_record(record_id: int, data: RecordUpdate, session: SessionDep, user: EditorUser) -> RecordOut:
    """Creator or admin only."""
    return RecordOut.model_validate(await records.update_record(session, user, record_id, data))


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT, responses={404: {"model": ErrorResponse, "description": "No such record"}})
async def delete_record(record_id: int, session: SessionDep, user: EditorUser) -> Response:
    """Creator or admin only."""
    await records.delete_record(session, user, record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
