"""
Pydantic models for the Harvest Agent configuration.

HarvestConfig  ←  loaded from / saved to  data/config/harvest_config.json

All search and business-filter parameters live here.
The backend NEVER hardcodes any of these values — it reads them exclusively
from the saved config that the UI Rule Engine writes.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
# Sources
# ══════════════════════════════════════════════════════════════════════════════

class SourcesConfig(BaseModel):
    """Toggle which job boards the agent harvests from."""
    linkedin: bool = True
    naukri:   bool = False
    dice:     bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Company Verification
# ══════════════════════════════════════════════════════════════════════════════

class VerificationConfig(BaseModel):
    """
    Company career-page verification settings.

    enabled        — run verification step after scraping
    method         — how to verify ("career_page" = visit company careers URL)
    on_mismatch    — action when career page found but job title not matched
    on_not_found   — action when career page cannot be reached
    """
    enabled:      bool                                  = False
    method:       Literal["career_page"]                = "career_page"
    on_mismatch:  Literal["skip", "flag", "include"]    = "flag"
    on_not_found: Literal["skip", "flag", "include"]    = "flag"


# ══════════════════════════════════════════════════════════════════════════════
# Filters (source of truth for ALL search + post-processing parameters)
# ══════════════════════════════════════════════════════════════════════════════

class FiltersConfig(BaseModel):
    """
    All search parameters and business-filter rules forwarded to every
    enabled source agent and the post-scraping filter pipeline.

    Values are set exclusively via the Rule Engine UI and persisted in
    harvest_config.json.  The backend never hardcodes any of these.
    """

    # ── Core search ───────────────────────────────────────────────────────────
    keyword:             str                                                                          = ""
    location:            str                                                                          = ""
    job_type:            Literal["Contract", "Permanent", "Part-time", "Freelance", "Full-time", "Any"] = "Any"
    work_mode:           Literal["Remote", "Hybrid", "Onsite", "Any"]                                = "Any"
    search_window_hours: Literal[24, 48, 72, 168, 720]                                               = 24
    # Safety cap — 0 means paginate until natural end of results.
    max_jobs:            int                                                                          = Field(500, ge=0, le=5000)

    # ── Domain ────────────────────────────────────────────────────────────────
    domain: Literal[
        "Data Engineering", "Data Science", "AI/ML", "SAP", "Cloud",
        "Digital", "UX/UI", "ERP", "Cyber Security", "Infrastructure",
        "IT", "Engineering", "Finance", "Operations", "Non-IT", "Any"
    ] = "Any"

    # ── Hiring entity + GCC ───────────────────────────────────────────────────
    hiring_entity: Literal["Direct Client", "GCC", "Ambiguous", "Staffing Firm", "Any"]             = "Any"
    gcc_mode:      Literal["include_gcc", "gcc_only", "exclude_gcc"]                                 = "include_gcc"

    # ── Salary filters ────────────────────────────────────────────────────────
    salary_min:                 int | None = None   # annual value in salary_currency
    salary_max:                 int | None = None
    salary_currency:            str        = "INR"  # ISO 4217
    include_undisclosed_salary: bool       = True   # keep jobs where salary is not disclosed

    # ── Company verification ──────────────────────────────────────────────────
    verification: VerificationConfig = Field(default_factory=VerificationConfig)


# ══════════════════════════════════════════════════════════════════════════════
# Schedule
# ══════════════════════════════════════════════════════════════════════════════

class ScheduleConfig(BaseModel):
    """APScheduler trigger definition."""
    frequency: Literal["hourly", "daily", "weekly"] = "daily"
    run_time:  str  = "09:00"          # HH:MM  (used when frequency == "daily" or "weekly")
    timezone:  str  = "Asia/Kolkata"   # IANA timezone name
    enabled:   bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Browser
# ══════════════════════════════════════════════════════════════════════════════

class BrowserConfig(BaseModel):
    """Playwright launch settings."""
    headless:       bool = False
    slow_mo_ms:     int  = Field(0, ge=0, le=3000)
    chrome_profile: str  = "data/chrome_profile"


# ══════════════════════════════════════════════════════════════════════════════
# Root config object
# ══════════════════════════════════════════════════════════════════════════════

class HarvestConfig(BaseModel):
    """Full harvest agent configuration — the root object in harvest_config.json."""
    sources:  SourcesConfig  = Field(default_factory=SourcesConfig)
    filters:  FiltersConfig  = Field(default_factory=FiltersConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    browser:  BrowserConfig  = Field(default_factory=BrowserConfig)
