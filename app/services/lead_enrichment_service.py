"""
Lead Enrichment Service — cross-source recruiter contact enrichment.

Strategy
────────
1. Build an index of Naukri jobs keyed by (recruiter_name, company) since
   Naukri is the richest source of email / phone data.
2. For every LinkedIn and Dice job that has a poster name but is missing
   email / phone / LinkedIn URL, look up the index and copy the contact
   data across.
3. Also attempt a company-only match when no name match is found.

Logs emitted
────────────
lead_enriched       — whenever a job gains new contact data
email_found         — email copied from another source
phone_found         — phone copied from another source
profile_matched     — name+company match found in Naukri index
"""
from __future__ import annotations

import re

import structlog

from app.models.unified_job import UnifiedJob

logger = structlog.get_logger(__name__)


def _normalize(text: str | None) -> str:
    """Lowercase, strip whitespace for fuzzy key matching."""
    return re.sub(r"\s+", "", (text or "").lower())


class LeadEnrichmentService:
    """
    Cross-source recruiter enrichment.

    Usage::

        enriched_jobs = LeadEnrichmentService().enrich(all_unified_jobs)
    """

    def enrich(self, jobs: list[UnifiedJob]) -> list[UnifiedJob]:
        """
        Enrich LinkedIn and Dice jobs with contact data from Naukri records
        that share the same recruiter name + company.
        """
        # ── Build lookup index from Naukri records ─────────────────────────────
        # Key: (name_norm, company_norm)
        name_company_index: dict[str, UnifiedJob] = {}
        # Secondary index: company-only, for cases where name is missing
        company_index: dict[str, list[UnifiedJob]] = {}

        for j in jobs:
            if j.source != "Naukri":
                continue
            if not j.job_poster_name:
                continue

            name_norm    = _normalize(j.job_poster_name)
            company_norm = _normalize(j.current_company or j.company)
            key = f"{name_norm}::{company_norm}"

            if key not in name_company_index:
                name_company_index[key] = j

            company_index.setdefault(company_norm, [])
            if j not in company_index[company_norm]:
                company_index[company_norm].append(j)

        logger.info(
            "lead_enrichment_started",
            naukri_indexed = len(name_company_index),
            total_jobs     = len(jobs),
        )

        enriched_count = 0

        for job in jobs:
            if job.source == "Naukri":
                continue

            changed = False

            # ── Primary match: name + company ──────────────────────────────────
            match: UnifiedJob | None = None

            if job.job_poster_name:
                name_norm    = _normalize(job.job_poster_name)
                company_norm = _normalize(job.current_company or job.company)
                key = f"{name_norm}::{company_norm}"
                match = name_company_index.get(key)

                if match:
                    logger.info(
                        "profile_matched",
                        source         = job.source,
                        name           = job.job_poster_name,
                        company        = job.current_company or job.company,
                        matched_source = "Naukri",
                    )

            # ── Secondary match: company-only (single unambiguous match) ────────
            if not match and job.company:
                company_norm  = _normalize(job.company)
                candidates    = company_index.get(company_norm, [])
                # Only use company match when exactly one Naukri record for that company
                # has contact data — prevents wrong merges for large firms
                contactable   = [
                    c for c in candidates
                    if c.email_id or c.contact_number
                ]
                if len(contactable) == 1:
                    match = contactable[0]

            if not match:
                continue

            # ── Copy missing fields ────────────────────────────────────────────
            if not job.email_id and match.email_id:
                job.email_id = match.email_id
                logger.info("email_found", source=job.source,
                            email=match.email_id, via="naukri_cross_enrich")
                changed = True

            if not job.contact_number and match.contact_number:
                job.contact_number = match.contact_number
                logger.info("phone_found", source=job.source,
                            phone=match.contact_number, via="naukri_cross_enrich")
                changed = True

            if not job.job_poster_designation and match.job_poster_designation:
                job.job_poster_designation = match.job_poster_designation
                changed = True

            if not job.linkedin_profile_url and match.linkedin_profile_url:
                job.linkedin_profile_url = match.linkedin_profile_url
                changed = True

            if not job.current_company and match.current_company:
                job.current_company = match.current_company
                changed = True

            if changed:
                enriched_count += 1
                logger.info(
                    "lead_enriched",
                    source  = job.source,
                    title   = job.job_title,
                    company = job.company,
                    email   = job.email_id,
                    phone   = job.contact_number,
                )

        logger.info(
            "lead_enrichment_complete",
            enriched = enriched_count,
            total    = len(jobs),
        )
        return jobs
