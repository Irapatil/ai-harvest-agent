"""
Models for the LinkedIn → Gemini enrichment pipeline.

Hierarchy
─────────
  LinkedInSearchConfig      caller-supplied search parameters + URL builder
  EnrichedLinkedInJob       one job: card data + description + Gemini parse
  LinkedInHarvestResult     full pipeline result (sync harvest endpoint)
  LinkedInSearchResult      card-only result (search-only endpoint)
  HarvestJob                background task tracking (async harvest endpoint)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.models.job_parser import EmploymentType, ParsedJobDescription, WorkMode


# ══════════════════════════════════════════════════════════════════════════════
# Filter enums
# ══════════════════════════════════════════════════════════════════════════════

class DatePosted(StrEnum):
    PAST_24H   = "past_24h"
    PAST_WEEK  = "past_week"
    PAST_MONTH = "past_month"
    ANY_TIME   = "any"


class WorkModeFilter(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    ANY    = "any"


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn URL filter mappings
# ══════════════════════════════════════════════════════════════════════════════

DATE_FILTER_MAP: dict[DatePosted, str] = {
    DatePosted.PAST_24H:   "r86400",    # 86 400 s = 24 h
    DatePosted.PAST_WEEK:  "r604800",   # 7 days
    DatePosted.PAST_MONTH: "r2592000",  # 30 days
    DatePosted.ANY_TIME:   "",
}

WORK_MODE_FILTER_MAP: dict[WorkModeFilter, str] = {
    WorkModeFilter.REMOTE: "2",
    WorkModeFilter.HYBRID: "3",
    WorkModeFilter.ONSITE: "1",
    WorkModeFilter.ANY:    "",
}

EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "contract":      "C",
    "permanent":     "F",
    "part_time":     "P",
    "freelance":     "C",
    "internship":    "I",
    "temporary":     "T",
    "not_specified": "",
}


# ══════════════════════════════════════════════════════════════════════════════
# Search configuration (request body for both harvest endpoints)
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInSearchConfig(BaseModel):
    """Controls every aspect of a LinkedIn harvest run."""

    keywords:           str            = Field(...,  description="Search terms, e.g. 'Contract Java Developer'")
    location:           str | None     = Field(None, description="City or country, e.g. 'London'. None = worldwide")
    date_posted:        DatePosted     = Field(DatePosted.PAST_24H,  description="How recent the postings should be")
    work_mode:          WorkModeFilter = Field(WorkModeFilter.ANY,   description="remote / hybrid / onsite / any")
    employment_type:    str            = Field("not_specified",       description="contract | permanent | part_time | freelance | internship | not_specified")
    max_jobs:           int            = Field(25,  ge=1,  le=100,   description="Max jobs to carry through the full pipeline")
    max_search_pages:   int            = Field(3,   ge=1,  le=10,    description="LinkedIn search pages to walk (25 jobs each)")
    fetch_descriptions: bool           = Field(True,                  description="Visit each job-detail page to extract the full description")
    parse_with_gemini:  bool           = Field(True,                  description="Send descriptions to Gemini for structured extraction")
    headless:           bool           = Field(True,                  description="Run Playwright headlessly")
    slow_mo_ms:         int            = Field(600, ge=0, le=3000,   description="Milliseconds between Playwright actions (anti-bot)")
    description_concurrency: int       = Field(3,   ge=1, le=8,      description="Parallel detail-page tabs")

    model_config = {
        "json_schema_extra": {
            "example": {
                "keywords": "Contract Java Developer",
                "date_posted": "past_24h",
                "work_mode": "any",
                "employment_type": "contract",
                "max_jobs": 25,
                "max_search_pages": 3,
                "fetch_descriptions": True,
                "parse_with_gemini": True,
                "headless": True,
                "slow_mo_ms": 600,
                "description_concurrency": 3,
            }
        }
    }

    def build_search_url(self) -> str:
        params: list[str] = [f"keywords={self.keywords.replace(' ', '%20')}"]

        if tpr := DATE_FILTER_MAP[self.date_posted]:
            params.append(f"f_TPR={tpr}")

        if wt := WORK_MODE_FILTER_MAP[self.work_mode]:
            params.append(f"f_WT={wt}")

        if jt := EMPLOYMENT_TYPE_MAP.get(self.employment_type, ""):
            params.append(f"f_JT={jt}")

        if self.location:
            params.append(f"location={self.location.replace(' ', '%20')}")

        params += ["position=1", "pageNum=0", "sortBy=DD"]
        return "https://www.linkedin.com/jobs/search/?" + "&".join(params)


# ══════════════════════════════════════════════════════════════════════════════
# Per-job result
# ══════════════════════════════════════════════════════════════════════════════

class EnrichedLinkedInJob(BaseModel):
    """
    One job posting after all three pipeline phases.

    Data provenance
    ───────────────
    Phase 1 (Playwright search)  → job_id, job_title, company, location, job_url, posted_time
    Phase 2 (Playwright detail)  → raw_description, description_length, description_fetch_error
    Phase 3 (Gemini parse)       → parsed, parse_error

    Smart-merge properties (no extra storage)
    ─────────────────────────────────────────
    effective_location      Gemini's location if present, else card's location
    effective_work_mode     Gemini's work_mode if resolved, else "not_specified"
    effective_employment    Gemini's employment_type if resolved, else config value
    effective_salary        Gemini's salary if present (no card-level fallback)
    effective_skills        Gemini's SkillSet if present, else None
    """

    # ── Phase 1: card ─────────────────────────────────────────────────────────
    job_id:      str
    job_title:   str
    company:     str
    location:    str                   # raw card location (city/country text)
    job_url:     str
    posted_time: str = ""

    # ── Phase 2: description ──────────────────────────────────────────────────
    raw_description:         str | None = None
    description_length:      int        = 0
    description_fetch_error: str | None = None

    # ── Phase 3: Gemini parse ─────────────────────────────────────────────────
    parsed:      ParsedJobDescription | None = None
    parse_error: str | None = None

    # ── Computed smart-merge helpers ──────────────────────────────────────────

    @property
    def is_fully_enriched(self) -> bool:
        return self.raw_description is not None and self.parsed is not None

    @property
    def effective_location(self) -> str:
        """Gemini location (more precise) when available; card location as fallback."""
        if self.parsed and self.parsed.location:
            return self.parsed.location
        return self.location

    @property
    def effective_work_mode(self) -> str:
        """Resolved work_mode from Gemini; 'not_specified' when Gemini had no data."""
        if self.parsed and self.parsed.work_mode != WorkMode.NOT_SPECIFIED:
            return self.parsed.work_mode.value
        return WorkMode.NOT_SPECIFIED.value

    @property
    def effective_employment_type(self) -> str:
        """Resolved employment_type from Gemini; 'not_specified' as fallback."""
        if self.parsed and self.parsed.employment_type != EmploymentType.NOT_SPECIFIED:
            return self.parsed.employment_type.value
        return EmploymentType.NOT_SPECIFIED.value

    @property
    def effective_salary(self):  # → SalaryRange | None
        """Salary extracted by Gemini, or None."""
        return self.parsed.salary if self.parsed else None

    @property
    def effective_skills(self):  # → SkillSet | None
        """SkillSet extracted by Gemini, or None."""
        return self.parsed.skills if self.parsed else None

    def summary(self) -> dict:
        """Compact dict for list views — no raw_description or full parse."""
        return {
            "job_id":               self.job_id,
            "job_title":            self.job_title,
            "company":              self.company,
            "effective_location":   self.effective_location,
            "effective_work_mode":  self.effective_work_mode,
            "effective_employment": self.effective_employment_type,
            "effective_salary":     self.effective_salary.model_dump() if self.effective_salary else None,
            "required_skills":      self.effective_skills.required if self.effective_skills else [],
            "job_url":              self.job_url,
            "posted_time":          self.posted_time,
            "confidence_score":     self.parsed.confidence_score if self.parsed else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Full pipeline result (sync harvest)
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInHarvestResult(BaseModel):
    """Returned by POST /api/v1/jobs/linkedin/harvest (sync) and stored by async jobs."""

    jobs:            list[EnrichedLinkedInJob]
    search_config:   LinkedInSearchConfig
    total_found:     int   = Field(description="Job cards collected by Playwright search")
    total_described: int   = Field(description="Jobs whose detail page was fetched")
    total_parsed:    int   = Field(description="Jobs successfully parsed by Gemini")
    duration_ms:     float
    errors:          list[str] = []


# ══════════════════════════════════════════════════════════════════════════════
# Search-only result (Phase 1 only, no Gemini cost)
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInSearchResult(BaseModel):
    """Returned by POST /api/v1/jobs/linkedin/search (no detail pages, no Gemini)."""

    jobs:          list[dict]
    keywords:      str
    total_found:   int
    pages_scraped: int
    duration_ms:   float


# ══════════════════════════════════════════════════════════════════════════════
# Background harvest job tracking (async harvest endpoint)
# ══════════════════════════════════════════════════════════════════════════════

class HarvestStatus(StrEnum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"


class HarvestJob(BaseModel):
    """Tracks the lifecycle of a background harvest task."""

    id:           str            = Field(default_factory=lambda: str(uuid4()))
    status:       HarvestStatus = HarvestStatus.PENDING
    config:       LinkedInSearchConfig
    result:       LinkedInHarvestResult | None = None
    error:        str | None     = None
    started_at:   datetime       = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds() * 1000

    model_config = {"arbitrary_types_allowed": True}


class HarvestJobSummary(BaseModel):
    """Lightweight status returned by POST /harvest/async and GET /harvest/{id}."""

    id:           str
    status:       HarvestStatus
    started_at:   datetime
    completed_at: datetime | None = None
    duration_ms:  float | None    = None
    total_found:  int | None      = None
    total_parsed: int | None      = None
    error:        str | None      = None

    @classmethod
    def from_job(cls, job: HarvestJob) -> "HarvestJobSummary":
        return cls(
            id           = job.id,
            status       = job.status,
            started_at   = job.started_at,
            completed_at = job.completed_at,
            duration_ms  = job.duration_ms,
            total_found  = job.result.total_found  if job.result else None,
            total_parsed = job.result.total_parsed if job.result else None,
            error        = job.error,
        )
