"""Bulk import of records from CSV or JSON. All rows are validated; any error rejects the file."""

import asyncio
import csv
import io
import json
import logging

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PayloadTooLargeError, ValidationFailedError
from app.core.time import to_storage
from app.models.record import Record
from app.models.user import User
from app.schemas.record import RecordCreate
from app.services import idempotency

logger = logging.getLogger("app.import")

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
TOO_LARGE = f"Upload exceeds the {MAX_BYTES // (1024 * 1024)} MB limit"
MAX_REPORTED_ERRORS = 50
REQUIRED_COLUMNS = {"title", "value", "category", "recorded_at", "server"}


# DictReader puts cells beyond the header under this key.
EXTRA_CELLS = "__extra_cells__"


def _detect_format(filename: str | None, content_type: str | None) -> str:
    name = (filename or "").lower()
    if name.endswith(".csv") or content_type in {"text/csv", "application/vnd.ms-excel"}:
        return "csv"
    if name.endswith(".json") or content_type == "application/json":
        return "json"
    raise ValidationFailedError("Upload a .csv or .json file")


def _rows_from_csv(text: str) -> list[tuple[int, dict]]:
    reader = csv.DictReader(io.StringIO(text), restkey=EXTRA_CELLS)
    try:
        names = [c.strip() for c in reader.fieldnames or []]
        columns = set(names)
        if len(columns) < len(names):
            raise ValidationFailedError("CSV has a column twice")
        if not columns:
            raise ValidationFailedError("File contains no records")
        unknown = columns - REQUIRED_COLUMNS
        if unknown:
            raise ValidationFailedError(f"CSV has unknown columns: {', '.join(sorted(unknown))}")
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValidationFailedError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        # Row 1 is the header, so data rows start at 2 as in a spreadsheet.
        return [(index, {k.strip(): v for k, v in row.items() if k}) for index, row in enumerate(reader, start=2)]
    except csv.Error as exc:  # e.g. a cell over the csv module's 128 KB field limit
        # line_num counts the lines read before the one that failed.
        raise ValidationFailedError(f"Invalid CSV at line {reader.line_num + 1}: {exc}") from exc


def _rows_from_json(text: str) -> list[tuple[int, dict]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError(f"Invalid JSON: {exc.msg} at line {exc.lineno}") from exc
    except RecursionError as exc:
        raise ValidationFailedError("Invalid JSON: nested too deeply") from exc
    except ValueError as exc:  # e.g. an integer over Python's 4,300-digit conversion limit
        raise ValidationFailedError("Invalid JSON: a number is too long") from exc
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise ValidationFailedError("JSON must be a list of records or an object with a 'records' list")
    return list(enumerate(payload, start=1))


def parse_records(raw: bytes, filename: str | None, content_type: str | None) -> list[RecordCreate]:
    if len(raw) > MAX_BYTES:
        raise PayloadTooLargeError(TOO_LARGE)
    try:
        text = raw.decode("utf-8-sig")  # tolerate the BOM Excel adds
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("File must be UTF-8 encoded") from exc
    fmt = _detect_format(filename, content_type)
    rows = _rows_from_csv(text) if fmt == "csv" else _rows_from_json(text)
    if not rows:
        raise ValidationFailedError("File contains no records")
    if len(rows) > MAX_ROWS:
        raise ValidationFailedError(f"File has {len(rows)} rows; the limit is {MAX_ROWS}")

    valid: list[RecordCreate] = []
    errors: list[dict] = []
    for row_number, row in rows:
        if not isinstance(row, dict):
            errors.append({"row": row_number, "field": None, "message": "Record must be an object"})
            continue
        if fmt == "csv" and EXTRA_CELLS in row:  # usually a comma in a value that is not in quotes
            message = f"Row has {len(row[EXTRA_CELLS])} more cell(s) than the header; put values with commas in quotes"
            errors.append({"row": row_number, "field": None, "message": message})
            continue
        try:
            valid.append(RecordCreate.model_validate(row))
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(part) for part in err["loc"]) or None
                errors.append({"row": row_number, "field": field, "message": err["msg"].removeprefix("Value error, ")})
    if errors:
        raise ValidationFailedError(
            f"{len(errors)} validation error(s); nothing was imported",
            errors=errors[:MAX_REPORTED_ERRORS],
        )
    return valid


def stage_records(session: AsyncSession, owner: User, records: list[RecordCreate]) -> None:
    """Add records to the session without committing."""
    session.add_all(
        Record(
            title=r.title,
            value=r.value,
            category=r.category,
            server=r.server,
            recorded_at=to_storage(r.recorded_at),
            owner_id=owner.id,
        )
        for r in records
    )


def log_import(owner: User, count: int) -> None:
    logger.info("Imported %d records", count, extra={"user_id": owner.id})


async def import_records(session: AsyncSession, owner: User, records: list[RecordCreate]) -> int:
    stage_records(session, owner, records)
    await session.commit()
    log_import(owner, len(records))
    return len(records)


async def import_file(
    session: AsyncSession, owner: User, raw: bytes, filename: str | None, content_type: str | None, key: str | None
) -> tuple[int, bool]:
    """Validate and import a file, at most once per Idempotency-Key. Returns (imported, replayed)."""
    # Parsing and validating up to 5,000 rows is CPU work: keep it off the event loop.
    rows = await asyncio.to_thread(parse_records, raw, filename, content_type)
    if key is None:
        return await import_records(session, owner, rows), False

    async def work() -> dict:
        stage_records(session, owner, rows)
        return {"imported": len(rows)}

    body, replayed = await idempotency.run_once(session, owner, key, idempotency.fingerprint("import_records", raw), work)
    if not replayed:
        log_import(owner, len(rows))
    return body["imported"], replayed
