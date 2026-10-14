from math import ceil

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[T], total: int, page: int, page_size: int) -> "Page[T]":
        return cls(items=items, total=total, page=page, page_size=page_size, pages=ceil(total / page_size) if total else 0)


class ErrorField(BaseModel):
    row: int | None = Field(None, description="Row number in an imported file, on import errors only")
    field: str | None = Field(description="Dotted path of the field, e.g. `title` or `records.3.value`")
    message: str


class ErrorResponse(BaseModel):
    """The body of every error response."""

    detail: str
    request_id: str | None = Field(description="Also in the X-Request-ID header; quote it when reporting a problem")
    errors: list[ErrorField] | None = Field(None, description="Per-field problems, on 422 only")
