"""
Recruiter / Prospect Intelligence — data models.

Email status (scraped-only — no prediction)
────────────────────────────────────────────
VERIFIED  — extracted from company website / corporate directory / press release
PUBLIC    — found on a public professional profile (LinkedIn, Naukri, Dice)
NOT_FOUND — not publicly available; nothing fabricated or guessed

Phone status
────────────
VERIFIED  — found on company website
PUBLIC    — found on public professional profile
NOT_FOUND — not publicly available; never predicted or generated
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Input (static Excel) ──────────────────────────────────────────────────────

@dataclass
class ProspectRecord:
    """Single row read from prospects.xlsx (after company forward-fill)."""
    company_name:      str
    person_name:       str
    designation:       str
    existing_linkedin: str = ""
    row_index:         int = 0


# ── Enriched output ────────────────────────────────────────────────────────────

@dataclass
class ProspectResult:
    """
    Fully enriched recruiter / prospect record.

    Confidence rules
    ────────────────
    High   — LinkedIn profile confirmed + VERIFIED or PUBLIC email + company match
    Medium — LinkedIn profile found + company match (no email found)
    Low    — No LinkedIn profile resolved

    CRITICAL: email and phone are NEVER predicted or generated.
    Only values actually scraped from a public source are stored.
    """
    # ── Input fields ──────────────────────────────────────────────────────────
    company_name: str
    person_name:  str
    designation:  str

    # ── Profile ───────────────────────────────────────────────────────────────
    linkedin_url:      str = ""
    location:          str = ""
    department:        str = ""
    linkedin_headline: str = ""   # scraped from profile — internal, not in Excel

    # ── Company ───────────────────────────────────────────────────────────────
    company_website: str = ""
    company_domain:  str = ""

    # ── Contact (scraped only — never fabricated) ──────────────────────────────
    official_email_id: str = ""
    email_status:      str = "NOT_FOUND"   # VERIFIED | PUBLIC | NOT_FOUND

    contact_number: str = ""
    phone_status:   str = "NOT_FOUND"      # VERIFIED | PUBLIC | NOT_FOUND

    # ── Hierarchy ─────────────────────────────────────────────────────────────
    reporting_hierarchy: str = ""

    # ── Extended profile intelligence ─────────────────────────────────────────
    position_level:     str = "NOT_FOUND"   # Recruiter | Senior Recruiter | Manager | Director | VP | Head | CHRO | Founder
    employment_type:    str = "NOT_FOUND"   # Full-time | Part-time | Contract | Freelance | Internship
    years_in_company:   str = "NOT_FOUND"   # "3 yrs 6 mos" — from current-role duration on LinkedIn
    overall_experience: str = "NOT_FOUND"   # total career experience summed from LinkedIn
    reporting_manager:  str = "NOT_FOUND"   # direct manager, if publicly visible
    hiring_domain:      str = "NOT_FOUND"   # AI/ML | Cloud/DevOps | Java | Data Engineering | …
    company_industry:   str = "NOT_FOUND"   # LinkedIn industry taxonomy
    company_size:       str = "NOT_FOUND"   # "1,001–5,000 employees" band from LinkedIn

    # ── Metadata ──────────────────────────────────────────────────────────────
    confidence_score: str = "Low"   # High | Medium | Low
    source:           str = ""      # pipe-separated sources used

    # ── Diagnostics (per enrichment step) ────────────────────────────────────
    profile_opened:        bool = False
    contact_section_found: bool = False
    email_found:           bool = False
    phone_found:           bool = False
    hierarchy_found:       bool = False
    enrichment_audit:      list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name":         self.company_name,
            "person_name":          self.person_name,
            "designation":          self.designation,
            "linkedin_url":         self.linkedin_url,
            "location":             self.location,
            "department":           self.department,
            "company_website":      self.company_website,
            "company_domain":       self.company_domain,
            "official_email_id":    self.official_email_id,
            "email_status":         self.email_status,
            "contact_number":       self.contact_number,
            "phone_status":         self.phone_status,
            "reporting_hierarchy":  self.reporting_hierarchy,
            "position_level":       self.position_level,
            "employment_type":      self.employment_type,
            "years_in_company":     self.years_in_company,
            "overall_experience":   self.overall_experience,
            "reporting_manager":    self.reporting_manager,
            "hiring_domain":        self.hiring_domain,
            "company_industry":     self.company_industry,
            "company_size":         self.company_size,
            "confidence_score":     self.confidence_score,
            "source":               self.source,
            # diagnostics
            "profile_opened":        self.profile_opened,
            "contact_section_found": self.contact_section_found,
            "email_found":           self.email_found,
            "phone_found":           self.phone_found,
            "hierarchy_found":       self.hierarchy_found,
            "enrichment_audit":      self.enrichment_audit,
        }


# ── Run summary ────────────────────────────────────────────────────────────────

@dataclass
class ProspectIntelligenceResult:
    """Summary returned by ProspectIntelligenceAgent.run()."""
    run_id:            str
    started_at:        str
    completed_at:      str
    runtime_minutes:   float
    total_prospects:   int
    enriched:          int
    high_confidence:   int
    medium_confidence: int
    low_confidence:    int
    verified_emails:   int
    public_emails:     int
    verified_phones:   int
    public_phones:     int
    no_contact:        int
    json_path:         str
    excel_path:        str
    results: list[ProspectResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id":            self.run_id,
            "started_at":        self.started_at,
            "completed_at":      self.completed_at,
            "runtime_minutes":   self.runtime_minutes,
            "total_prospects":   self.total_prospects,
            "enriched":          self.enriched,
            "high_confidence":   self.high_confidence,
            "medium_confidence": self.medium_confidence,
            "low_confidence":    self.low_confidence,
            "verified_emails":   self.verified_emails,
            "public_emails":     self.public_emails,
            "verified_phones":   self.verified_phones,
            "public_phones":     self.public_phones,
            "no_contact":        self.no_contact,
            "json_path":         self.json_path,
            "excel_path":        self.excel_path,
        }
