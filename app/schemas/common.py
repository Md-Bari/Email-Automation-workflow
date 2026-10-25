from __future__ import annotations

import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[Any], total: int, page: int, page_size: int) -> Page[T]:
        return cls(items=items, total=total, page=page, page_size=page_size,
                   pages=math.ceil(total / page_size) if page_size else 0)


class PageParams(BaseModel):
    page: int = Field(1, ge=1, le=10_000)
    page_size: int = Field(20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class MessageResponse(BaseModel):
    message: str


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    404: {"model": ErrorResponse, "description": "Not found or not accessible"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}
