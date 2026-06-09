"""Standard API response envelope models."""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """Generic success envelope: {"data": ..., "message": ""}"""

    data: T
    message: str = "success"


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated list response."""

    items: list[T]
    total: int
    page: int
    page_size: int
    has_next: bool
    has_prev: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthStatus(BaseModel):
    status: str  # "ok" | "degraded" | "down"
    version: str
    environment: str
    checks: dict[str, str] = {}
