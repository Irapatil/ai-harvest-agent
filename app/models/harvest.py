"""Harvest job and result models (SQLAlchemy ORM + Pydantic schemas)."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ── ORM Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────────────────────────────

class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── ORM Models ───────────────────────────────────────────────────────────────────

class HarvestJobORM(Base):
    __tablename__ = "harvest_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str] = mapped_column(String(50), default="harvest")
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING)
    max_pages: Mapped[int] = mapped_column(default=10)
    output_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    results: Mapped[list[HarvestResultORM]] = relationship(back_populates="job", lazy="selectin")


class HarvestResultORM(Base):
    __tablename__ = "harvest_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("harvest_jobs.id"), nullable=False)
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int] = mapped_column(default=1)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    job: Mapped[HarvestJobORM] = relationship(back_populates="results")


# ── Pydantic Schemas ─────────────────────────────────────────────────────────────

class HarvestJobCreate(BaseModel):
    url: str = Field(..., description="Starting URL to harvest")
    goal: str = Field(..., description="Natural language description of what to extract")
    agent_type: str = Field("harvest", description="Agent type: harvest | scraper")
    max_pages: int = Field(10, ge=1, le=100, description="Max pages to visit")
    output_schema: dict[str, Any] | None = Field(
        None, description="Optional JSON schema describing desired output structure"
    )


class HarvestJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    goal: str
    agent_type: str
    status: JobStatus
    max_pages: int
    output_schema: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class HarvestResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    page_url: str
    page_number: int
    raw_content: str | None = None
    extracted_data: dict[str, Any] | None = None
    screenshot_path: str | None = None
    created_at: datetime


class HarvestJobWithResults(HarvestJob):
    results: list[HarvestResult] = []
