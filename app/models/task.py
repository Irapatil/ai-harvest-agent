"""Celery task tracking models."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REVOKED = "REVOKED"
    RETRY = "RETRY"


class Task(BaseModel):
    """Representation of a Celery background task."""

    task_id: str
    task_name: str
    status: TaskStatus
    job_id: str | None = None
    result: Any | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: int = Field(0, ge=0, le=100)
    meta: dict[str, Any] = Field(default_factory=dict)


class TaskList(BaseModel):
    tasks: list[Task]
    total: int
    page: int
    page_size: int
