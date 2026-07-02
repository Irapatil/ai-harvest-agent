"""
Lead Merge Agent — merges LinkedIn + Premium Naukri data and deduplicates.

Merge rules
───────────
Primary dedup key:  LinkedIn profile URL (normalised)
Secondary key:      normalised(first_name + last_name) :: normalised(company)

Merge strategy
──────────────
1. LinkedIn is the base record (source of profile URL and post URL).
2. Premium Naukri fills in any missing fields (email, phone, designation,
   location, employment_history, current_company).
3. If both sources have a value, LinkedIn wins for profile identity fields;
   Naukri wins for contact fields (email, phone) since it shows premium data.
4. `source` list is de-duped and sorted.

This module is pure Python — no I/O, no Playwright, no async.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import structlog

from app.models.lead_models import LeadRecord, LinkedInPost, NaukriProfile

logger = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation helpers
# ══════════════════════════════════════════════════════════════════════════════

def _norm_url(url: str) -> str:
    """Strip query string and trailing slash from a URL for stable comparison."""
    if not url:
        return ""
    try:
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(p._replace(query="", fragment="")).rstrip("/").lower()
    except Exception:
        return url.lower().strip()


def _norm_name(name: str) -> str:
    """Lower + collapse whitespace."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _norm_company(company: str) -> str:
    """Strip common legal suffixes for fuzzy matching."""
    cleaned = re.sub(
        r"\b(pvt|ltd|limited|inc|llc|corp|technologies|tech|solutions|services)\b",
        "",
        company.lower(),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _name_company_key(name: str, company: str) -> str:
    parts = _norm_name(name).split()
    first = parts[0] if parts else ""
    last  = parts[-1] if len(parts) > 1 else ""
    return f"{first}:{last}::{_norm_company(company)}"


def _infer_department(designation: str) -> str:
    """Classify department from designation string."""
    d = designation.lower()
    if any(k in d for k in ("chief human", "chro", "head of hr", "vp hr", "vp people")):
        return "HR Leadership"
    if any(k in d for k in ("talent acquisition", "ta ", "recruiter", "recruitment", "sourcing")):
        return "Talent Acquisition"
    if any(k in d for k in ("hr", "human resource", "people")):
        return "Human Resources"
    if any(k in d for k in ("business development", "bd", "growth")):
        return "Business Development"
    if any(k in d for k in ("founder", "co-founder", "ceo", "managing director", "director")):
        return "Leadership"
    return "Talent Acquisition"  # default for LinkedIn hiring posts


def _parse_company_from_headline(headline: str) -> tuple[str, str]:
    """Return (designation, company) from 'X at Y' or 'X | Y' format."""
    for sep in (" at ", " @ ", " | ", " - "):
        if sep in headline:
            parts = headline.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return headline.strip(), ""


# ══════════════════════════════════════════════════════════════════════════════
# Lead Merge Agent
# ══════════════════════════════════════════════════════════════════════════════

class LeadMergeAgent:
    """
    Pure-Python agent that merges LinkedIn posts + Naukri profiles into
    de-duplicated LeadRecord instances.
    """

    # ── Public entry points ────────────────────────────────────────────────────

    def build_from_linkedin(self, post: LinkedInPost) -> LeadRecord:
        """
        Create an initial LeadRecord from a LinkedIn post.
        Contact status is PUBLIC if explicitly in post text, else NOT_FOUND.
        """
        designation, company = _parse_company_from_headline(post.author_headline)

        email        = post.raw_email.strip()
        phone        = post.raw_phone.strip()
        email_status = "PUBLIC"  if email else "NOT_FOUND"
        phone_status = "PUBLIC"  if phone else "NOT_FOUND"

        record = LeadRecord(
            recruiter_name       = post.author_name,
            designation          = designation,
            department           = _infer_department(designation),
            company              = company or post.author_company,
            current_company      = company or post.author_company,
            location             = "",
            linkedin_profile_url = post.author_profile_url,
            job_post_url         = post.post_url,
            official_email       = email,
            email_status         = email_status,   # type: ignore[arg-type]
            contact_number       = phone,
            phone_status         = phone_status,   # type: ignore[arg-type]
            source               = ["LinkedIn"],
            last_verified        = datetime.now(timezone.utc).isoformat(),
        )
        return record

    def enrich_with_naukri(
        self,
        record:  LeadRecord,
        naukri:  NaukriProfile,
        company: str = "",
    ) -> LeadRecord:
        """
        Fill missing fields in `record` using data from a Premium Naukri profile.
        Returns the mutated record.
        """
        # Email — Naukri contact is premium data
        if not record.official_email and naukri.email:
            record.official_email = naukri.email
            record.email_status   = self._classify_email(naukri.email, company or naukri.current_company)  # type: ignore[assignment]

        # Phone
        if not record.contact_number and naukri.phone:
            record.contact_number = naukri.phone
            record.phone_status   = "PUBLIC"  # type: ignore[assignment]

        # Designation / company
        if not record.designation and naukri.designation:
            record.designation   = naukri.designation
            record.department    = _infer_department(naukri.designation)
        if not record.current_company and naukri.current_company:
            record.current_company = naukri.current_company
        if not record.company:
            record.company = naukri.current_company

        # Location
        if not record.location and naukri.location:
            record.location = naukri.location

        # Employment history
        if not record.employment_history and naukri.employment_history:
            record.employment_history = naukri.employment_history

        # LinkedIn URL from Naukri (if not already set)
        if not record.linkedin_profile_url and naukri.linkedin_url:
            record.linkedin_profile_url = naukri.linkedin_url

        # Source list
        if "Premium Naukri" not in record.source:
            record.source.append("Premium Naukri")

        record.last_verified = datetime.now(timezone.utc).isoformat()
        return record

    def deduplicate(self, records: list[LeadRecord]) -> list[LeadRecord]:
        """
        Deduplicate a list of LeadRecords.

        Primary key:   normalised LinkedIn profile URL
        Secondary key: normalised name :: normalised company
        When duplicates are found, the record with more data wins (richer merge).
        """
        url_index:  dict[str, int] = {}   # url_key → index in `out`
        name_index: dict[str, int] = {}   # name_company_key → index in `out`
        out:        list[LeadRecord] = []

        for rec in records:
            url_key  = _norm_url(rec.linkedin_profile_url)
            name_key = _name_company_key(rec.recruiter_name, rec.current_company or rec.company)

            existing_idx: int | None = None
            if url_key and url_key in url_index:
                existing_idx = url_index[url_key]
            elif name_key and name_key in name_index:
                existing_idx = name_index[name_key]

            if existing_idx is not None:
                out[existing_idx] = self._merge_records(out[existing_idx], rec)
            else:
                idx = len(out)
                out.append(rec)
                if url_key:
                    url_index[url_key] = idx
                name_index[name_key] = idx

        logger.info(
            "merge_completed",
            before = len(records),
            after  = len(out),
            deduped = len(records) - len(out),
        )
        return out

    # ── Internal ───────────────────────────────────────────────────────────────

    def _merge_records(self, primary: LeadRecord, secondary: LeadRecord) -> LeadRecord:
        """Merge secondary into primary — primary wins on identity, secondary fills gaps."""
        # Contact — take the richer value
        if not primary.official_email and secondary.official_email:
            primary.official_email = secondary.official_email
            primary.email_status   = secondary.email_status  # type: ignore[assignment]
        if not primary.contact_number and secondary.contact_number:
            primary.contact_number = secondary.contact_number
            primary.phone_status   = secondary.phone_status  # type: ignore[assignment]

        # Profile fields
        for field in ("designation", "department", "location", "current_company"):
            if not getattr(primary, field) and getattr(secondary, field):
                setattr(primary, field, getattr(secondary, field))

        if not primary.employment_history and secondary.employment_history:
            primary.employment_history = secondary.employment_history

        # Merge source lists
        combined = list(dict.fromkeys(primary.source + secondary.source))  # preserve order, dedup
        primary.source = combined

        return primary

    @staticmethod
    def _classify_email(email: str, company: str) -> str:
        """Heuristic: VERIFIED if email domain contains company name slug."""
        if not email or "@" not in email:
            return "NOT_FOUND"
        domain = email.split("@", 1)[-1].lower()
        slug   = re.sub(r"[^a-z0-9]", "", company.lower())[:8]
        if len(slug) > 3 and slug in domain:
            return "VERIFIED"
        return "PUBLIC"
