"""
UnifiedJob — common internal representation used across all source agents,
business-filter pipeline, and verification agent.

Every source (LinkedIn, Naukri, Dice, …) converts its scraped records to
this type before any post-processing.  The API response models are built
from UnifiedJob at the route layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UnifiedJob:
    """
    Single job listing in a source-agnostic format.

    Fields populated by source agents
    ──────────────────────────────────
    job_title, company, location, salary, experience, posted_date,
    job_url, job_description, skills, work_mode, source

    Fields populated by BusinessFilterService (after scraping)
    ──────────────────────────────────────────────────────────
    domain, hiring_entity, is_gcc, job_type (tagged from config)

    Fields populated by VerificationAgent (optional)
    ─────────────────────────────────────────────────
    verification_status
    """

    # ── Source agent fills these ──────────────────────────────────────────────
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    skills:          list[str]
    work_mode:       str        # "remote" | "hybrid" | "onsite" | "not_specified"
    source:          str        # "LinkedIn" | "Naukri" | "Dice"

    # ── BusinessFilterService fills these ─────────────────────────────────────
    job_type:       str  = ""           # "Contract" | "Permanent" | … (from config)
    domain:         str  = "Any"        # "IT" | "Finance" | "Engineering" | …
    hiring_entity:  str  = "Any"        # "Direct Client" | "GCC" | "Staffing Firm" | "Ambiguous"
    is_gcc:         bool = False

    # ── VerificationAgent fills this ──────────────────────────────────────────
    # "pending" | "verified" | "not_verified" | "career_page_not_found" | "skipped"
    verification_status: str = "pending"

    # ── Lead Intelligence (populated by source agents, optional) ─────────────
    job_poster_name:        str | None = None   # Recruiter / Hiring Manager name
    job_poster_designation: str | None = None   # Recruiter title / designation
    linkedin_profile_url:   str | None = None   # Recruiter LinkedIn profile URL
    current_company:        str | None = None   # Recruiter's current company
    email_id:               str | None = None   # Recruiter email
    contact_number:         str | None = None   # Recruiter phone / mobile

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_title":              self.job_title,
            "company":                self.company,
            "location":               self.location,
            "salary":                 self.salary,
            "experience":             self.experience,
            "posted_date":            self.posted_date,
            "job_url":                self.job_url,
            "job_description":        self.job_description,
            "skills":                 self.skills,
            "work_mode":              self.work_mode,
            "source":                 self.source,
            "job_type":               self.job_type,
            "domain":                 self.domain,
            "hiring_entity":          self.hiring_entity,
            "is_gcc":                 self.is_gcc,
            "verification_status":    self.verification_status,
            "job_poster_name":        self.job_poster_name,
            "job_poster_designation": self.job_poster_designation,
            "linkedin_profile_url":   self.linkedin_profile_url,
            "current_company":        self.current_company,
            "email_id":               self.email_id,
            "contact_number":         self.contact_number,
        }
