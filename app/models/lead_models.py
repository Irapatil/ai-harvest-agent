"""
Lead Intelligence data models — Hybrid LinkedIn + Premium Naukri workflow.

Output contract
───────────────
Every LeadRecord.to_output_dict() exactly matches the specified CRM-ready JSON schema:
  lead_id, recruiter_name, designation, department, company, current_company,
  location, linkedin_profile_url, job_post_url, official_email, email_status,
  contact_number, phone_status, employment_history, source, confidence_score,
  last_verified, crm_status

Data integrity rules (enforced at model level)
───────────────────────────────────────────────
• official_email  — ONLY if actually scraped. Never fabricated. Never predicted.
• contact_number  — ONLY if actually scraped. Never fabricated.
• email_status    — VERIFIED | PUBLIC | NOT_FOUND  (nothing else)
• phone_status    — VERIFIED | PUBLIC | NOT_FOUND  (nothing else)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
# Intermediate scraping containers (internal — not part of output schema)
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInPost(BaseModel):
    """Raw data scraped from one LinkedIn hiring post."""
    post_url:           str = ""
    author_name:        str = ""
    author_profile_url: str = ""
    author_headline:    str = ""
    author_company:     str = ""
    post_content:       str = ""
    post_date:          str = ""
    raw_email:          str = ""   # extracted from post text — PUBLIC only
    raw_phone:          str = ""   # extracted from post text — PUBLIC only


class NaukriProfile(BaseModel):
    """Raw data extracted from a Premium Naukri recruiter profile page."""
    profile_url:        str       = ""
    recruiter_name:     str       = ""
    designation:        str       = ""
    current_company:    str       = ""
    location:           str       = ""
    email:              str       = ""
    phone:              str       = ""
    employment_history: list[str] = Field(default_factory=list)
    linkedin_url:       str       = ""
    resume_url:         str       = ""
    profile_summary:    str       = ""


# ══════════════════════════════════════════════════════════════════════════════
# Lead Intelligence Config
# ══════════════════════════════════════════════════════════════════════════════

class LeadIntelligenceRequest(BaseModel):
    """
    FastAPI request body for POST /run-lead-intelligence.

    All fields optional — lead_intelligence_config.json provides defaults.
    """
    keyword:                    str       = Field(
        "AI Engineer",
        description="Search keyword for LinkedIn post discovery (e.g. 'AI Engineer', 'Python')",
        examples=["AI Engineer"],
    )
    max_leads:                  int       = Field(
        50,
        ge=1, le=500,
        description="Max recruiter leads to collect and enrich",
    )
    search_sources:             list[str] = Field(
        ["linkedin", "premium_naukri"],
        description="Sources to use: linkedin | premium_naukri",
    )
    fallback_to_premium_naukri: bool      = Field(
        True,
        description="If True, falls back to Premium Naukri when LinkedIn yields no contact details",
    )
    minimum_confidence:         float     = Field(
        0.70,
        ge=0.0, le=1.0,
        description="Minimum confidence score to include a lead in the CRM dataset",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "keyword":                    "AI Engineer",
                "max_leads":                  50,
                "search_sources":             ["linkedin", "premium_naukri"],
                "fallback_to_premium_naukri": True,
                "minimum_confidence":         0.70,
            }
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# Core output record — CRM-ready
# ══════════════════════════════════════════════════════════════════════════════

class LeadRecord(BaseModel):
    """
    Single consolidated lead in CRM-ready format.

    Exactly matches the output JSON schema specified in the product requirement.
    The field `confidence_value` (float) is internal only and excluded from output.
    """

    lead_id:              str = Field(
        default_factory=lambda: uuid.uuid4().hex[:16],
        description="Stable unique identifier for this lead",
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    recruiter_name:       str       = ""
    designation:          str       = ""
    department:           str       = ""
    company:              str       = ""
    current_company:      str       = ""
    location:             str       = ""

    # ── Digital footprint ────────────────────────────────────────────────────
    linkedin_profile_url: str       = ""
    job_post_url:         str       = ""

    # ── Contact (SCRAPED ONLY — no fabrication) ───────────────────────────────
    official_email:       str       = ""
    email_status: Literal["VERIFIED", "PUBLIC", "NOT_FOUND"] = "NOT_FOUND"
    contact_number:       str       = ""
    phone_status: Literal["VERIFIED", "PUBLIC", "NOT_FOUND"] = "NOT_FOUND"

    # ── Enrichment ────────────────────────────────────────────────────────────
    employment_history:   list[str] = Field(default_factory=list)
    source:               list[str] = Field(default_factory=list)

    # ── Quality ───────────────────────────────────────────────────────────────
    confidence_score: Literal["High", "Medium", "Low"] = "Low"
    confidence_value: float = Field(0.0, exclude=True)   # internal — not in output
    last_verified:    str   = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    crm_status:       str   = "Ready"

    def to_output_dict(self) -> dict[str, Any]:
        """Return dict matching the CRM-ready JSON schema (internal fields excluded)."""
        return {
            "lead_id":              self.lead_id,
            "recruiter_name":       self.recruiter_name,
            "designation":          self.designation,
            "department":           self.department,
            "company":              self.company,
            "current_company":      self.current_company,
            "location":             self.location,
            "linkedin_profile_url": self.linkedin_profile_url,
            "job_post_url":         self.job_post_url,
            "official_email":       self.official_email,
            "email_status":         self.email_status,
            "contact_number":       self.contact_number,
            "phone_status":         self.phone_status,
            "employment_history":   self.employment_history,
            "source":               self.source,
            "confidence_score":     self.confidence_score,
            "last_verified":        self.last_verified,
            "crm_status":           self.crm_status,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator result container
# ══════════════════════════════════════════════════════════════════════════════

class LeadIntelligenceResult:
    """Mutable result container built by LeadIntelligenceOrchestrator.run()."""

    def __init__(self) -> None:
        self.run_id:                   str        = ""
        self.executed_at:              str        = ""
        self.keyword:                  str        = ""
        self.total_leads:              int        = 0
        self.high_confidence:          int        = 0
        self.medium_confidence:        int        = 0
        self.low_confidence:           int        = 0
        self.sources_used:             list[str]  = []
        self.linkedin_posts_found:     int        = 0
        self.recruiters_extracted:     int        = 0
        self.premium_naukri_fallbacks: int        = 0
        self.leads:                    list[LeadRecord] = []
        self.json_path:                str        = ""
        self.excel_path:               str        = ""
        self.started_at:               datetime | None = None
        self.completed_at:             datetime | None = None

    @property
    def runtime_minutes(self) -> float:
        if self.started_at and self.completed_at:
            return round(
                (self.completed_at - self.started_at).total_seconds() / 60, 2
            )
        return 0.0

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "run_id":                   self.run_id,
            "executed_at":              self.executed_at,
            "keyword":                  self.keyword,
            "total_leads":              self.total_leads,
            "high_confidence":          self.high_confidence,
            "medium_confidence":        self.medium_confidence,
            "low_confidence":           self.low_confidence,
            "sources_used":             self.sources_used,
            "linkedin_posts_found":     self.linkedin_posts_found,
            "recruiters_extracted":     self.recruiters_extracted,
            "premium_naukri_fallbacks": self.premium_naukri_fallbacks,
            "json_path":                self.json_path,
            "excel_path":               self.excel_path,
            "runtime_minutes":          self.runtime_minutes,
        }
