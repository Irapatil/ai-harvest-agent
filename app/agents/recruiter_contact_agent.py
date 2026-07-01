"""
Recruiter Contact Discovery Agent

Reads job-poster / recruiter records from completed harvest runs
(LinkedIn, Naukri, Dice combined JSON files), deduplicates them,
then enriches each with scraped contact discovery.

Input sources scanned
─────────────────────
data/results/combined/*_combined.json   ← primary (all sources merged)
data/results/linkedin/*_linkedin.json   ← LinkedIn-only fallback
data/results/naukri/*_naukri.json       ← Naukri-only fallback
data/results/dice/*_dice.json           ← Dice-only fallback

Deduplication
─────────────
Primary key  : linkedin_profile_url    (when non-null)
Secondary key: lower(name) + lower(company)
Job titles posted by each recruiter are aggregated.

Enrichment pipeline (per recruiter)
─────────────────────────────────────
Step 1 — Company website scraping          → VERIFIED email / phone
Step 2 — LinkedIn profile visit            → PUBLIC (click Contact Info modal)
Step 3 — Naukri cross-source validation    → PUBLIC
Step 4 — Hierarchy discovery               → DuckDuckGo
Step 5 — Confidence scoring

CRITICAL RULES
──────────────
• Email and phone are NEVER predicted or generated.
• email_status: VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• phone_status: VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• Chrome profile already authenticated — no login automation.
• Debug JSON saved per run to data/results/lead_intelligence/debug/
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.models.prospect_models import ProspectResult
from app.agents.prospect_intelligence_agent import (
    _infer_company_domain,
    _infer_department,
    _score_confidence,
    _save_intermediate,
    _save_debug_json,
    _ddg_linkedin_search,
    _scrape_company_website_contacts,
    _extract_linkedin_contact_info,
    _extract_linkedin_profile_metadata,
    _search_naukri_contact,
    _ddg_hierarchy_search,
    _classify_position_level,
    _classify_hiring_domain,
)

logger = structlog.get_logger(__name__)

_RESULTS_ROOT        = Path("data/results")
_OUTPUT_DIR          = Path("data/results/lead_intelligence")
_INTERMEDIATE_DIR    = _OUTPUT_DIR / "intermediate"
_INTERMEDIATE_EVERY  = 25
_DEFAULT_CONCURRENCY = 2

_SOURCE_DIRS: dict[str, str] = {
    "combined": "combined",
    "linkedin":  "linkedin",
    "naukri":    "naukri",
    "dice":      "dice",
}

_SOURCE_SUFFIX: dict[str, str] = {
    "combined": "_combined.json",
    "linkedin":  "_linkedin.json",
    "naukri":    "_naukri.json",
    "dice":      "_dice.json",
}


# ── Input record (harvest-derived) ────────────────────────────────────────────

@dataclass
class RecruiterRecord:
    """One unique recruiter extracted from one or more harvest runs."""
    person_name:       str
    company_name:      str
    designation:       str  = ""
    existing_linkedin: str  = ""
    harvest_source:    str  = ""
    job_titles_posted: list[str] = field(default_factory=list)
    run_ids:           list[str] = field(default_factory=list)


# ── Run summary ────────────────────────────────────────────────────────────────

@dataclass
class RecruiterDiscoveryResult:
    """Summary returned by RecruiterContactAgent.run()."""
    run_id:            str
    started_at:        str
    completed_at:      str
    runtime_minutes:   float
    harvest_sources:   list[str]
    total_recruiters:  int
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
    debug_path:        str = ""
    results: list[ProspectResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id":            self.run_id,
            "started_at":        self.started_at,
            "completed_at":      self.completed_at,
            "runtime_minutes":   self.runtime_minutes,
            "harvest_sources":   self.harvest_sources,
            "total_recruiters":  self.total_recruiters,
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
            "debug_path":        self.debug_path,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Harvest loader
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_name(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    return re.sub(r'\s+', ' ', cleaned).strip()


def _load_recruiters_from_harvest(
    source_filter: str = "all",
    run_ids: list[str] | None = None,
    max_files: int = 10,
) -> list[RecruiterRecord]:
    """
    Scan harvest result JSON files and return unique RecruiterRecord list.

    Deduplication
    ─────────────
    1. linkedin_profile_url (if non-null) → unique key
    2. lower(name) + "|" + lower(company)
    """
    dirs_to_scan = (
        ["combined", "linkedin", "naukri", "dice"] if source_filter == "all"
        else [source_filter] if source_filter in _SOURCE_DIRS
        else ["combined"]
    )

    seen:    dict[str, RecruiterRecord] = {}
    li_urls: set[str]                   = set()
    files_loaded: list[str]             = []

    for src_key in dirs_to_scan:
        src_dir = _RESULTS_ROOT / _SOURCE_DIRS[src_key]
        suffix  = _SOURCE_SUFFIX[src_key]
        if not src_dir.exists():
            continue

        json_files = sorted(
            src_dir.glob(f"*{suffix}"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:max_files]

        for jf in json_files:
            if run_ids and not any(jf.name.startswith(rid) for rid in run_ids):
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:
                logger.warning("harvest_read_error", file=str(jf), error=str(exc))
                continue

            files_loaded.append(jf.name)
            harvest_run_id = data.get("run_id", jf.stem)

            for job in data.get("jobs", []):
                raw_name = (job.get("job_poster_name") or "").strip()
                if not raw_name:
                    continue

                name        = _clean_name(raw_name)
                company     = _clean_name(job.get("company") or job.get("current_company") or "")
                desig_raw   = _clean_name(job.get("job_poster_designation") or "")
                li_url      = (job.get("linkedin_profile_url") or "").strip()
                job_title   = _clean_name(job.get("job_title") or "")
                harvest_src = _clean_name(job.get("source") or src_key.capitalize())

                # Scraper artifact: designation often mirrors the name field
                designation = desig_raw if desig_raw.lower() != name.lower() else ""

                if li_url and li_url not in li_urls:
                    key = li_url
                    li_urls.add(li_url)
                elif li_url and li_url in li_urls:
                    rec = seen.get(li_url)
                    if rec and job_title and job_title not in rec.job_titles_posted:
                        rec.job_titles_posted.append(job_title)
                    continue
                else:
                    key = f"{name.lower()}|{company.lower()}"

                if key in seen:
                    rec = seen[key]
                    if job_title and job_title not in rec.job_titles_posted:
                        rec.job_titles_posted.append(job_title)
                    if harvest_run_id not in rec.run_ids:
                        rec.run_ids.append(harvest_run_id)
                else:
                    seen[key] = RecruiterRecord(
                        person_name       = name,
                        company_name      = company,
                        designation       = designation,
                        existing_linkedin = li_url,
                        harvest_source    = harvest_src,
                        job_titles_posted = [job_title] if job_title else [],
                        run_ids           = [harvest_run_id],
                    )

    recruiters = list(seen.values())
    logger.info(
        "recruiters_loaded",
        total=len(recruiters), files_loaded=len(files_loaded), sources=dirs_to_scan,
    )
    return recruiters


# ═══════════════════════════════════════════════════════════════════════════════
# RecruiterContactAgent
# ═══════════════════════════════════════════════════════════════════════════════

class RecruiterContactAgent:
    """
    Orchestrates scraped contact discovery for recruiters from harvest results.

    Usage::

        agent  = RecruiterContactAgent(concurrency=2)
        result = await agent.run(source_filter="all")
    """

    def __init__(self, concurrency: int = _DEFAULT_CONCURRENCY) -> None:
        self._concurrency = max(1, min(concurrency, 5))

    async def run(
        self,
        source_filter: str = "all",
        run_ids: list[str] | None = None,
        max_files: int = 10,
    ) -> RecruiterDiscoveryResult:
        started_at = datetime.now(timezone.utc)
        run_id     = "rcd_" + started_at.strftime("%Y%m%d_%H%M%S")

        logger.info("recruiter_discovery_start", run_id=run_id, source_filter=source_filter)

        recruiters = _load_recruiters_from_harvest(
            source_filter=source_filter, run_ids=run_ids, max_files=max_files,
        )

        if not recruiters:
            completed_at = datetime.now(timezone.utc)
            return RecruiterDiscoveryResult(
                run_id=run_id, started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(), runtime_minutes=0.0,
                harvest_sources=[source_filter], total_recruiters=0,
                enriched=0, high_confidence=0, medium_confidence=0, low_confidence=0,
                verified_emails=0, public_emails=0,
                verified_phones=0, public_phones=0, no_contact=0,
                json_path="", excel_path="",
            )

        all_results: list[ProspectResult] = []

        from app.services.config_service import ConfigService
        from app.scrapers.browser_manager import PersistentBrowserManager

        config = ConfigService().load()
        sem    = asyncio.Semaphore(self._concurrency)

        async def _enrich_one(rec: RecruiterRecord) -> ProspectResult:
            async with sem:
                page = await pbm.new_page()
                try:
                    return await self._enrich_recruiter(page, rec)
                except Exception as exc:
                    logger.warning("recruiter_enrich_error", person=rec.person_name, error=str(exc))
                    domain, website = _infer_company_domain(rec.company_name)
                    return ProspectResult(
                        company_name     = rec.company_name,
                        person_name      = rec.person_name,
                        designation      = rec.designation,
                        company_domain   = domain,
                        company_website  = website,
                        email_status     = "NOT_FOUND",
                        phone_status     = "NOT_FOUND",
                        department       = _infer_department(rec.designation),
                        confidence_score = "Low",
                        source           = rec.harvest_source or "Harvest",
                        enrichment_audit = [f"Exception: {exc}"],
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        async with PersistentBrowserManager(
            profile_dir = config.browser.chrome_profile,
            headless    = config.browser.headless,
            slow_mo     = config.browser.slow_mo_ms,
        ) as pbm:
            BATCH = _INTERMEDIATE_EVERY
            for batch_start in range(0, len(recruiters), BATCH):
                batch         = recruiters[batch_start: batch_start + BATCH]
                batch_tasks   = [_enrich_one(r) for r in batch]
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                for i, r in enumerate(batch_results):
                    if isinstance(r, Exception):
                        rec             = batch[i]
                        domain, website = _infer_company_domain(rec.company_name)
                        all_results.append(ProspectResult(
                            company_name     = rec.company_name,
                            person_name      = rec.person_name,
                            designation      = rec.designation,
                            company_domain   = domain,
                            company_website  = website,
                            email_status     = "NOT_FOUND",
                            phone_status     = "NOT_FOUND",
                            department       = _infer_department(rec.designation),
                            confidence_score = "Low",
                            source           = rec.harvest_source or "Harvest",
                            enrichment_audit = [f"gather exception: {r}"],
                        ))
                    else:
                        all_results.append(r)  # type: ignore[arg-type]

                batch_num = batch_start // BATCH + 1
                _save_intermediate(all_results, batch_num, run_id)
                logger.info(
                    "recruiter_batch_complete",
                    batch=batch_num, batch_count=len(batch),
                    total_done=len(all_results), total=len(recruiters),
                )

        json_path  = self._save_json(all_results, run_id, source_filter)
        debug_path = _save_debug_json(all_results, run_id)
        excel_path = ""
        try:
            from app.services.prospect_excel_service import ProspectExcelService
            excel_path = ProspectExcelService().export(
                all_results, run_id, report_title="Recruiter Contact Report"
            )
        except Exception as exc:
            logger.warning("recruiter_excel_export_failed", error=str(exc))

        try:
            summary_path = _OUTPUT_DIR / "recruiter_discovery_summary.json"
            summary_path.write_text(
                json.dumps({"run_id": run_id, "total": len(all_results)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

        completed_at    = datetime.now(timezone.utc)
        elapsed_seconds = (completed_at - started_at).total_seconds()

        unique_sources = list({r.harvest_source for r in recruiters if r.harvest_source})

        result = RecruiterDiscoveryResult(
            run_id            = run_id,
            started_at        = started_at.isoformat(),
            completed_at      = completed_at.isoformat(),
            runtime_minutes   = round(elapsed_seconds / 60, 1),
            harvest_sources   = unique_sources,
            total_recruiters  = len(recruiters),
            enriched          = sum(1 for r in all_results if r.linkedin_url or r.company_domain),
            high_confidence   = sum(1 for r in all_results if r.confidence_score == "High"),
            medium_confidence = sum(1 for r in all_results if r.confidence_score == "Medium"),
            low_confidence    = sum(1 for r in all_results if r.confidence_score == "Low"),
            verified_emails   = sum(1 for r in all_results if r.email_status == "VERIFIED"),
            public_emails     = sum(1 for r in all_results if r.email_status == "PUBLIC"),
            verified_phones   = sum(1 for r in all_results if r.phone_status == "VERIFIED"),
            public_phones     = sum(1 for r in all_results if r.phone_status == "PUBLIC"),
            no_contact        = sum(
                1 for r in all_results
                if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
            ),
            json_path  = json_path,
            excel_path = excel_path,
            debug_path = debug_path,
            results    = all_results,
        )

        logger.info(
            "recruiter_discovery_complete",
            run_id          = run_id,
            total           = result.total_recruiters,
            verified_emails = result.verified_emails,
            public_emails   = result.public_emails,
            no_contact      = result.no_contact,
            runtime_minutes = result.runtime_minutes,
        )
        return result

    # ── Per-recruiter enrichment ───────────────────────────────────────────────

    async def _enrich_recruiter(self, page: Any, rec: RecruiterRecord) -> ProspectResult:
        """
        Scraped contact discovery for one recruiter.

        Step 1 — Company website (VERIFIED)
        Step 2 — LinkedIn profile — click Contact Info modal (PUBLIC)
        Step 3 — Naukri cross-validation (PUBLIC)
        Step 4 — Hierarchy discovery
        Step 5 — Confidence scoring

        No email or phone prediction. NOT_FOUND = not publicly available.
        """
        t0      = time.monotonic()
        result  = ProspectResult(
            company_name = rec.company_name,
            person_name  = rec.person_name,
            designation  = rec.designation,
        )
        sources: list[str] = [rec.harvest_source] if rec.harvest_source else []
        audit:   list[str] = [
            f"Harvest source: {rec.harvest_source}",
            f"Jobs posted: {', '.join(rec.job_titles_posted[:3])}" if rec.job_titles_posted else "No job titles",
        ]

        logger.info("enriching_recruiter", person=rec.person_name, company=rec.company_name, has_li=bool(rec.existing_linkedin))

        # ── Company domain ─────────────────────────────────────────────────────
        domain, website = _infer_company_domain(rec.company_name)
        result.company_domain  = domain
        result.company_website = website

        # ══════════════════════════════════════════════════════════════════════
        # Step 1 — Company website (VERIFIED)
        # ══════════════════════════════════════════════════════════════════════
        if website:
            try:
                contact = await _scrape_company_website_contacts(
                    page, website, rec.person_name, domain
                )
                if contact["email"]:
                    result.official_email_id = contact["email"]
                    result.email_status      = "VERIFIED"
                    sources.append("Company Website")
                    audit.append(f"S1 VERIFIED email: {contact['email']} (page:{contact['source_page']})")
                else:
                    audit.append(f"S1 website scraped — no personal email (domain:{domain})")
                if contact["phone"]:
                    result.contact_number = contact["phone"]
                    result.phone_status   = "VERIFIED"
                    sources.append("Company Website (Phone)")
                    audit.append(f"S1 VERIFIED phone: {contact['phone']}")
            except Exception as exc:
                audit.append(f"S1 error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 2 — LinkedIn profile (PUBLIC)
        # ══════════════════════════════════════════════════════════════════════
        try:
            linkedin_url = rec.existing_linkedin

            if not linkedin_url:
                ddg          = await _ddg_linkedin_search(page, rec.person_name, rec.company_name)
                linkedin_url = ddg.get("linkedin_url", "")
                result.linkedin_headline = ddg.get("headline", "")
                if linkedin_url:
                    sources.append("DuckDuckGo")
                    audit.append(f"S2 LinkedIn URL via DDG: {linkedin_url}")
                else:
                    audit.append("S2 DDG LinkedIn: no profile found")

            if linkedin_url:
                result.linkedin_url  = linkedin_url
                contact_info = await _extract_linkedin_contact_info(page, linkedin_url, domain)

                result.profile_opened        = contact_info["profile_opened"]
                result.contact_section_found = contact_info["contact_section_found"]

                if contact_info["headline"] and not result.linkedin_headline:
                    result.linkedin_headline = contact_info["headline"]
                if contact_info["location"]:
                    result.location = contact_info["location"]

                if contact_info["email"] and result.email_status != "VERIFIED":
                    result.official_email_id = contact_info["email"]
                    result.email_status      = "PUBLIC"
                    sources.append("LinkedIn Profile")
                    audit.append(f"S2 PUBLIC email from LinkedIn: {contact_info['email']}")
                else:
                    audit.append(
                        f"S2 LinkedIn visited "
                        f"(profile_opened:{contact_info['profile_opened']}, "
                        f"contact_section:{contact_info['contact_section_found']}) — no email"
                    )

                if contact_info["phone"] and result.phone_status != "VERIFIED":
                    result.contact_number = contact_info["phone"]
                    result.phone_status   = "PUBLIC"
                    sources.append("LinkedIn Profile (Phone)")
                    audit.append(f"S2 PUBLIC phone from LinkedIn: {contact_info['phone']}")

                # Extended metadata — scraped from the already-loaded profile page
                if result.profile_opened:
                    try:
                        meta = await _extract_linkedin_profile_metadata(page)
                        if meta["employment_type"] != "NOT_FOUND":
                            result.employment_type = meta["employment_type"]
                        if meta["years_in_company"] != "NOT_FOUND":
                            result.years_in_company = meta["years_in_company"]
                        if meta["overall_experience"] != "NOT_FOUND":
                            result.overall_experience = meta["overall_experience"]
                        if meta["company_industry"] != "NOT_FOUND":
                            result.company_industry = meta["company_industry"]
                        if meta["company_size"] != "NOT_FOUND":
                            result.company_size = meta["company_size"]
                        audit.append(
                            f"S2 metadata: emp_type={meta['employment_type']}, "
                            f"tenure={meta['years_in_company']}, "
                            f"industry={meta['company_industry']}, "
                            f"size={meta['company_size']}"
                        )
                    except Exception as exc:
                        audit.append(f"S2 metadata error: {exc}")

        except Exception as exc:
            audit.append(f"S2 LinkedIn error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 3 — Naukri cross-source (PUBLIC)
        # ══════════════════════════════════════════════════════════════════════
        if result.email_status == "NOT_FOUND" or result.phone_status == "NOT_FOUND":
            try:
                naukri = await _search_naukri_contact(
                    page, rec.person_name, rec.company_name, domain
                )
                if naukri["profile_url"]:
                    sources.append("Naukri")
                    audit.append(f"S3 Naukri profile: {naukri['profile_url']}")
                if naukri["email"] and result.email_status == "NOT_FOUND":
                    result.official_email_id = naukri["email"]
                    result.email_status      = "PUBLIC"
                    audit.append(f"S3 PUBLIC email from Naukri: {naukri['email']}")
                if naukri["phone"] and result.phone_status == "NOT_FOUND":
                    result.contact_number = naukri["phone"]
                    result.phone_status   = "PUBLIC"
                    audit.append(f"S3 PUBLIC phone from Naukri: {naukri['phone']}")
                if not naukri["profile_url"]:
                    audit.append("S3 Naukri: no profile found")
            except Exception as exc:
                audit.append(f"S3 Naukri error: {exc}")

        # ── Diagnostics ────────────────────────────────────────────────────────
        result.email_found = result.email_status in ("VERIFIED", "PUBLIC")
        result.phone_found = result.phone_status in ("VERIFIED", "PUBLIC")

        # ══════════════════════════════════════════════════════════════════════
        # Step 4 — Hierarchy discovery
        # ══════════════════════════════════════════════════════════════════════
        if rec.company_name:
            try:
                hierarchy              = await _ddg_hierarchy_search(page, rec.company_name)
                result.reporting_hierarchy = hierarchy
                result.hierarchy_found     = bool(hierarchy)
                if hierarchy:
                    sources.append("DDG Hierarchy")
                    audit.append(f"S4 Hierarchy: {hierarchy[:100]}")
                else:
                    audit.append("S4 Hierarchy: none found")
            except Exception as exc:
                audit.append(f"S4 hierarchy error: {exc}")

        # ── Department ─────────────────────────────────────────────────────────
        result.department = _infer_department(rec.designation, result.linkedin_headline)

        # ── Position level (seniority tier) ────────────────────────────────────
        result.position_level = _classify_position_level(rec.designation, result.linkedin_headline)

        # ── Hiring domain (from designation + headline + job titles posted) ─────
        result.hiring_domain = _classify_hiring_domain(
            rec.designation, result.linkedin_headline, rec.job_titles_posted
        )

        # ── Confidence ─────────────────────────────────────────────────────────
        result.confidence_score = _score_confidence(result, rec.company_name, rec.designation)
        result.source           = ", ".join(dict.fromkeys(sources))
        result.enrichment_audit = audit

        elapsed = round(time.monotonic() - t0, 1)
        logger.info(
            "recruiter_enriched",
            person=rec.person_name, linkedin=bool(result.linkedin_url),
            email_status=result.email_status, phone_status=result.phone_status,
            confidence=result.confidence_score, duration_s=elapsed,
        )
        return result

    def _save_json(self, results: list[ProspectResult], run_id: str, source_filter: str) -> str:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path    = _OUTPUT_DIR / f"{run_id}_recruiter_contacts.json"
        payload = {
            "run_id":          run_id,
            "source_filter":   source_filter,
            "total":           len(results),
            "enriched":        sum(1 for r in results if r.linkedin_url or r.company_domain),
            "high":            sum(1 for r in results if r.confidence_score == "High"),
            "medium":          sum(1 for r in results if r.confidence_score == "Medium"),
            "low":             sum(1 for r in results if r.confidence_score == "Low"),
            "verified_emails": sum(1 for r in results if r.email_status == "VERIFIED"),
            "public_emails":   sum(1 for r in results if r.email_status == "PUBLIC"),
            "verified_phones": sum(1 for r in results if r.phone_status == "VERIFIED"),
            "public_phones":   sum(1 for r in results if r.phone_status == "PUBLIC"),
            "no_contact":      sum(
                1 for r in results
                if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
            ),
            "results": [r.to_dict() for r in results],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path.resolve())
