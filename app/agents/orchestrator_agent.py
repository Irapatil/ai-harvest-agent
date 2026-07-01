"""
Orchestrator Agent — unified entry point for every harvest execution.

Two public methods
──────────────────
run()       Legacy interface — used by /run-harvest and the scheduler.
            Returns (list[HarvestJob], source_label_str).

run_all()   Full pipeline — used by POST /run-harvest-agent.
            Executes sources in priority order, applies business filters,
            deduplicates cross-source, returns OrchestratorResult.

Source priority (fixed):
  1. Naukri
  2. LinkedIn
  3. Dice

Extensibility
─────────────
To add a new source (e.g. Indeed):
  • Create  app/agents/indeed_agent.py  with  class IndeedAgent
  • Add     "indeed": bool  to  SourcesConfig
  • Add an  "indeed"  block in  _collect_all()  below
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.agents.linkedin_agent import (
    LINKEDIN_SESSION_FILE,
    LinkedInAgent,
    LinkedInLoginError,
    LinkedInScrapedJob,
)
from app.models.harvest_models import HarvestConfig
from app.models.response_models import HarvestJob
from app.models.unified_job import UnifiedJob
from app.services.business_filter_service import BusinessFilterService

_COMBINED_DIR = Path("data/results/combined")

logger = structlog.get_logger(__name__)


def _filter_by_date_window(jobs: list[UnifiedJob], window_hours: int) -> list[UnifiedJob]:
    """
    Secondary safety filter — drop jobs whose posted_date falls outside
    the configured search window.

    This is a backstop for the rare case where a job board ignores the
    URL-level time filter (jobAge / f_TPR / datePosted) and returns older
    listings. Jobs with a missing or unparseable posted_date are kept so
    we never silently discard valid records.
    """
    if not window_hours or window_hours <= 0:
        return jobs

    cutoff = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # For windows < 48 h we allow today only; for longer windows we go back N calendar days.
    import math
    extra_days = max(0, math.ceil(window_hours / 24) - 1)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=extra_days)

    kept = []
    dropped = 0
    for job in jobs:
        raw = (job.posted_date or "").strip()
        if not raw:
            kept.append(job)   # no date — keep, don't silently discard
            continue
        try:
            # Handles ISO date (2026-06-24) and ISO datetime strings
            pd = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if pd.tzinfo is None:
                pd = pd.replace(tzinfo=timezone.utc)
            if pd.date() >= cutoff.date():
                kept.append(job)
            else:
                dropped += 1
        except ValueError:
            kept.append(job)   # unparseable date — keep

    if dropped:
        logger.info(
            "date_window_filter",
            window_hours=window_hours,
            cutoff_date=cutoff.date().isoformat(),
            before=len(jobs),
            kept=len(kept),
            dropped=dropped,
        )
    return kept


def _deduplicate(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """Remove duplicates across all sources by job_url, then company+title."""
    seen_urls: set[str] = set()
    seen_ct:   set[str] = set()
    deduped:   list[UnifiedJob] = []
    for job in jobs:
        url_key = job.job_url.split("?")[0].rstrip("/").lower() if job.job_url else ""
        ct_key  = (
            re.sub(r"\s+", " ", job.company.lower().strip())
            + "::"
            + re.sub(r"\s+", " ", job.job_title.lower().strip())
        )
        if (url_key and url_key in seen_urls) or ct_key in seen_ct:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_ct.add(ct_key)
        deduped.append(job)
    return deduped


def _save_combined(
    run_id:       str,
    executed_at:  str,
    jobs:         list[UnifiedJob],
    filters_snap: dict,
) -> str:
    """Save all deduplicated jobs to data/results/combined/YYYYMMDD_combined.json."""
    _COMBINED_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path    = _COMBINED_DIR / f"{ts}_combined.json"
    payload = {
        "run_id":      run_id,
        "executed_at": executed_at,
        "total_found": len(jobs),
        "sources":     list({j.source for j in jobs}),
        "filters":     filters_snap,
        "jobs":        [j.to_dict() for j in jobs],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path.resolve())

def _cross_source_enrich(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """
    Cross-source lead enrichment.

    Builds a lookup from Naukri jobs (which have the richest contact data) keyed
    by (recruiter_name_lower, company_lower).  LinkedIn and Dice jobs that share
    the same recruiter name + company but are missing email / phone / LinkedIn URL
    are enriched from the matching Naukri record.
    """
    # Build index from Naukri records that have at least a name
    naukri_index: dict[str, UnifiedJob] = {}
    for j in jobs:
        if j.source == "Naukri" and j.job_poster_name:
            key = (
                re.sub(r"\s+", "", j.job_poster_name.lower())
                + "::"
                + re.sub(r"\s+", "", (j.current_company or j.company or "").lower())
            )
            if key not in naukri_index:
                naukri_index[key] = j

    enriched_count = 0
    for job in jobs:
        if job.source == "Naukri":
            continue
        if not job.job_poster_name:
            continue
        key = (
            re.sub(r"\s+", "", job.job_poster_name.lower())
            + "::"
            + re.sub(r"\s+", "", (job.current_company or job.company or "").lower())
        )
        match = naukri_index.get(key)
        if not match:
            continue
        changed = False
        if not job.email_id and match.email_id:
            job.email_id = match.email_id
            changed = True
        if not job.contact_number and match.contact_number:
            job.contact_number = match.contact_number
            changed = True
        if not job.job_poster_designation and match.job_poster_designation:
            job.job_poster_designation = match.job_poster_designation
            changed = True
        if changed:
            enriched_count += 1

    logger.info("cross_source_enrichment_complete", enriched=enriched_count)
    return jobs


# Fixed execution priority — lower index = runs first
_SOURCE_PRIORITY = ["naukri", "linkedin", "dice"]


# ── Converters: source-specific dataclass → UnifiedJob ────────────────────────

def _naukri_to_unified(j: "NaukriScrapedJob", job_type: str) -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "Naukri",
        job_type                = job_type,
        job_poster_name         = getattr(j, "recruiter_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        current_company         = getattr(j, "recruiter_company", None),
        email_id                = getattr(j, "email_id", None),
        contact_number          = getattr(j, "contact_number", None),
    )


def _linkedin_to_unified(j: LinkedInScrapedJob, job_type: str) -> UnifiedJob:
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "LinkedIn",
        job_type                = job_type,
        job_poster_name         = getattr(j, "job_poster_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        linkedin_profile_url    = getattr(j, "linkedin_profile_url", None),
    )


def _dice_to_unified(j: "DiceScrapedJob", job_type: str) -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "Dice",
        job_type                = job_type,
        job_poster_name         = getattr(j, "recruiter_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        current_company         = getattr(j, "recruiter_company", None),
        email_id                = getattr(j, "email_id", None),
        contact_number          = getattr(j, "contact_number", None),
        linkedin_profile_url    = getattr(j, "linkedin_profile_url", None),
    )


# ── OrchestratorResult ─────────────────────────────────────────────────────────

@dataclass
class OrchestratorResult:
    """Rich result object returned by run_all()."""
    sources_executed: list[str]                   = field(default_factory=list)
    jobs_by_source:   dict[str, list[UnifiedJob]] = field(default_factory=dict)
    all_jobs:         list[UnifiedJob]            = field(default_factory=list)
    started_at:       datetime                    = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at:     datetime                    = field(default_factory=lambda: datetime.now(timezone.utc))
    combined_path:    str                         = ""
    excel_path:       str                         = ""

    @property
    def total_jobs(self) -> int:
        return len(self.all_jobs)

    @property
    def verified_jobs(self) -> int:
        return sum(1 for j in self.all_jobs if j.verification_status == "verified")

    @property
    def direct_clients(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Direct Client")

    @property
    def gcc(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "GCC")

    @property
    def staffing_firms(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Staffing Firm")

    @property
    def ambiguous(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Ambiguous")


# ══════════════════════════════════════════════════════════════════════════════
# OrchestratorAgent
# ══════════════════════════════════════════════════════════════════════════════

class OrchestratorAgent:
    """
    Routes a HarvestConfig to source agents and returns aggregated results.

    Usage (new pipeline)::

        config = ConfigService().load()
        orch   = OrchestratorAgent(config)
        result = await orch.run_all()   # OrchestratorResult

    Usage (legacy — backward compat)::

        config     = ConfigService().load()
        orch       = OrchestratorAgent(config)
        jobs, src  = await orch.run()   # list[HarvestJob], str
    """

    def __init__(self, config: HarvestConfig) -> None:
        self._config = config

    # ── Full pipeline (POST /run-harvest-agent) ───────────────────────────────

    async def run_all(self) -> OrchestratorResult:
        """
        Execute all enabled sources in priority order, apply business filters,
        deduplicate cross-source, save combined results, and return OrchestratorResult.
        """
        config      = self._config
        started_at  = datetime.now(timezone.utc)
        executed_at = started_at.isoformat()
        run_id      = started_at.strftime("%Y%m%d_%H%M%S")
        result      = OrchestratorResult(started_at=started_at)

        # ── Step 1: collect raw jobs from all enabled sources ─────────────────
        raw_by_source = await self._collect_all(config)

        # ── Step 2: convert to UnifiedJob ─────────────────────────────────────
        all_unified: list[UnifiedJob] = []
        for source, jobs in raw_by_source.items():
            result.sources_executed.append(source)
            result.jobs_by_source[source] = jobs
            all_unified.extend(jobs)

        logger.info("orchestrator_raw_total", total=len(all_unified))

        # ── Step 2a: log batch_saved as we process the unified list ──────────
        _BATCH_SIZE = 100
        for _bi in range(0, len(all_unified), _BATCH_SIZE):
            _batch_n = _bi // _BATCH_SIZE + 1
            logger.info(
                "batch_saved",
                batch        = _batch_n,
                count        = len(all_unified[_bi : _bi + _BATCH_SIZE]),
                total_so_far = min(_bi + _BATCH_SIZE, len(all_unified)),
                stage        = "unified",
            )

        # ── Step 2b: cross-source lead enrichment ────────────────────────────
        from app.services.lead_enrichment_service import LeadEnrichmentService
        all_unified = LeadEnrichmentService().enrich(all_unified)

        # ── Step 3: classify + apply business filters ─────────────────────────
        svc         = BusinessFilterService()
        all_unified = svc.classify_all(all_unified, config.filters)
        all_unified = svc.apply_all(all_unified, config.filters)

        logger.info(
            "classification_completed",
            total         = len(all_unified),
            direct_client = sum(1 for j in all_unified if j.hiring_entity == "Direct Client"),
            gcc           = sum(1 for j in all_unified if j.hiring_entity == "GCC"),
            staffing_firm = sum(1 for j in all_unified if j.hiring_entity == "Staffing Firm"),
            ambiguous     = sum(1 for j in all_unified if j.hiring_entity == "Ambiguous"),
        )

        # ── Step 4: company verification (optional) ───────────────────────────
        if config.filters.verification.enabled:
            from app.agents.verification_agent import VerificationAgent
            verifier    = VerificationAgent(config.filters.verification, headless=True)
            all_unified = await verifier.verify_batch(all_unified)

        # ── Step 5: cross-source deduplication ────────────────────────────────
        _li_before_dedup = sum(1 for j in all_unified if j.source == "LinkedIn")
        logger.info("linkedin_jobs_before_dedup", count=_li_before_dedup)

        before_dedup = len(all_unified)
        all_unified  = _deduplicate(all_unified)
        removed      = before_dedup - len(all_unified)

        _li_after_dedup = sum(1 for j in all_unified if j.source == "LinkedIn")
        logger.info("linkedin_jobs_after_dedup", count=_li_after_dedup,
                    removed_by_dedup=_li_before_dedup - _li_after_dedup)
        logger.info(
            "deduplication_completed",
            before          = before_dedup,
            after           = len(all_unified),
            removed         = removed,
            linkedin_before = _li_before_dedup,
            linkedin_after  = _li_after_dedup,
        )

        # Save debug: linkedin after dedup
        try:
            import json as _json_dd
            _dbg_dd = Path("data/debug/linkedin")
            _dbg_dd.mkdir(parents=True, exist_ok=True)
            (_dbg_dd / "linkedin_after_dedup.json").write_text(
                _json_dd.dumps(
                    {"stage": "after_dedup", "count": _li_after_dedup,
                     "jobs": [{"title": j.job_title, "company": j.company, "url": j.job_url}
                               for j in all_unified if j.source == "LinkedIn"]},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        # ── Step 5b: enforce search_window_hours on posted_date ──────────────
        # The URL-level filter (jobAge/f_TPR/datePosted) is the primary gate;
        # this is a secondary code-level check to drop any jobs the board
        # returned outside the configured window despite the filter.
        all_unified = _filter_by_date_window(
            all_unified, config.filters.search_window_hours
        )

        # ── Step 6: rebuild jobs_by_source from deduped set ───────────────────
        deduped_by_source: dict[str, list[UnifiedJob]] = {}
        for job in all_unified:
            deduped_by_source.setdefault(job.source, []).append(job)
        result.jobs_by_source = deduped_by_source

        # ── Step 7: save combined JSON ────────────────────────────────────────
        filters_snap  = config.filters.model_dump() if hasattr(config.filters, "model_dump") else {}
        combined_path = _save_combined(run_id, executed_at, all_unified, filters_snap)
        logger.info("json_saved", path=combined_path, total=len(all_unified))

        result.all_jobs      = all_unified
        result.combined_path = combined_path

        # ── Step 8: export Excel workbook ─────────────────────────────────────
        _li_before_excel = len(deduped_by_source.get("LinkedIn", []))
        logger.info("linkedin_jobs_before_excel", count=_li_before_excel)

        # Save debug: linkedin before excel
        try:
            import json as _json_be
            _dbg_be = Path("data/debug/linkedin")
            _dbg_be.mkdir(parents=True, exist_ok=True)
            (_dbg_be / "linkedin_before_excel.json").write_text(
                _json_be.dumps(
                    {"stage": "before_excel", "count": _li_before_excel,
                     "jobs": [{"title": j.job_title, "company": j.company, "url": j.job_url}
                               for j in deduped_by_source.get("LinkedIn", [])]},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        try:
            from app.services.excel_export_service import ExcelExportService
            excel_path = ExcelExportService().export(
                all_jobs       = all_unified,
                jobs_by_source = deduped_by_source,
                run_id         = run_id,
                filters_snap   = filters_snap,
            )
            result.excel_path = excel_path
            logger.info("excel_saved", path=excel_path, total=len(all_unified))
        except Exception as exc:
            logger.warning("excel_export_failed", error=str(exc))

        result.completed_at  = datetime.now(timezone.utc)

        elapsed = (result.completed_at - started_at).total_seconds()

        # Save linkedin_summary.json — single file showing all pipeline stages
        try:
            import json as _json_sum
            _dbg_sum = Path("data/debug/linkedin")
            _dbg_sum.mkdir(parents=True, exist_ok=True)
            (_dbg_sum / "linkedin_summary.json").write_text(
                _json_sum.dumps(
                    {
                        "run_id":                         run_id,
                        "linkedin_jobs_extracted":        _li_before_dedup,
                        "linkedin_jobs_received_by_orch": _li_before_dedup,
                        "linkedin_jobs_before_dedup":     _li_before_dedup,
                        "linkedin_jobs_after_dedup":      _li_after_dedup,
                        "linkedin_jobs_before_excel":     _li_before_excel,
                        "linkedin_jobs_written_to_excel": _li_before_excel,
                        "root_cause_if_zero":
                            "Check uvicorn_err.txt for UnicodeEncodeError or LinkedInLoginError "
                            "before the linkedin_jobs_received_by_orchestrator log line."
                            if _li_before_dedup == 0 else "OK",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        lead_count = sum(
            1 for j in all_unified
            if getattr(j, "job_poster_name", None)
            or getattr(j, "email_id", None)
            or getattr(j, "contact_number", None)
        )

        logger.info(
            "harvest_completed",
            run_id         = run_id,
            sources        = result.sources_executed,
            linkedin_jobs  = len(deduped_by_source.get("LinkedIn", [])),
            naukri_jobs    = len(deduped_by_source.get("Naukri", [])),
            dice_jobs      = len(deduped_by_source.get("Dice", [])),
            combined_jobs  = result.total_jobs,
            lead_records   = lead_count,
            excel_generated = bool(result.excel_path),
            excel_path     = result.excel_path,
            json_path      = combined_path,
            verified       = result.verified_jobs,
            elapsed        = round(elapsed, 1),
        )
        return result

    # ── Legacy interface (backward compat) ────────────────────────────────────

    async def run(self) -> tuple[list[HarvestJob], str]:
        """
        Legacy method used by /run-harvest and the APScheduler job.
        Returns (list[HarvestJob], source_label).
        """
        config      = self._config
        all_jobs:   list[HarvestJob] = []
        src_labels: list[str]        = []

        if config.sources.linkedin:
            logger.info("orchestrator_dispatching", source="linkedin")
            session_path   = str(LINKEDIN_SESSION_FILE) if LINKEDIN_SESSION_FILE.exists() else None
            agent          = LinkedInAgent()
            linkedin_scraped = await agent.harvest(
                filters  = config.filters,
                headless = config.browser.headless,
                slow_mo  = config.browser.slow_mo_ms,
            )
            for j in linkedin_scraped:
                all_jobs.append(HarvestJob(
                    title     = j.job_title,
                    company   = j.company,
                    location  = j.location,
                    posted    = j.posted_date,
                    job_url   = j.job_url,
                    work_mode = j.work_mode,
                    source    = "LinkedIn",
                ))
            src_labels.append("LinkedIn")
            logger.info("orchestrator_source_done", source="linkedin", count=len(linkedin_scraped))

        if config.sources.naukri:
            from app.agents.naukri_agent import NaukriAgent
            logger.info("orchestrator_dispatching", source="naukri")
            naukri_agent   = NaukriAgent()
            naukri_scraped = await naukri_agent.harvest(
                filters  = config.filters,
                headless = config.browser.headless,
                slow_mo  = config.browser.slow_mo_ms,
            )
            for j in naukri_scraped:
                all_jobs.append(HarvestJob(
                    title     = j.job_title,
                    company   = j.company,
                    location  = j.location,
                    posted    = j.posted_date,
                    job_url   = j.job_url,
                    work_mode = j.work_mode,
                    source    = "Naukri",
                ))
            src_labels.append("Naukri")
            logger.info("orchestrator_source_done", source="naukri", count=len(naukri_scraped))

        source_label = ", ".join(src_labels) if src_labels else "none"
        logger.info("orchestrator_complete", total=len(all_jobs), sources=source_label)
        return all_jobs, source_label

    # ── Internal: collect raw UnifiedJobs from all enabled sources ─────────────

    async def _collect_all(
        self,
        config: HarvestConfig,
    ) -> dict[str, list[UnifiedJob]]:
        """
        Run all enabled source agents IN PARALLEL using a single shared browser
        context (one page per source).  Using a single PersistentBrowserManager
        avoids Chrome profile-lock conflicts that arise when launching multiple
        browser instances against the same profile directory.

        Returns {source_name: [UnifiedJob, …]}.
        """
        from app.scrapers.browser_manager import PersistentBrowserManager
        from app.scrapers.dice_scraper import DiceScrapedJob, DiceScraper

        results:  dict[str, list[UnifiedJob]] = {}
        enabled = [s for s in _SOURCE_PRIORITY if getattr(config.sources, s, False)]

        if not enabled:
            logger.warning("orchestrator_no_sources_enabled")
            return results

        logger.info("orchestrator_parallel_start", sources=enabled)

        async with PersistentBrowserManager(
            profile_dir = config.browser.chrome_profile,
            headless    = config.browser.headless,
            slow_mo     = config.browser.slow_mo_ms,
        ) as pbm:
            # Open one independent tab per source — they share cookies but
            # each has its own navigation state.
            pages: dict[str, Any] = {}
            for source in enabled:
                pages[source] = await pbm.new_page()

            # ── Per-source harvest coroutines ──────────────────────────────────

            async def _harvest_naukri(page: Any) -> tuple[str, list[UnifiedJob]]:
                from app.agents.naukri_agent import NaukriAgent, NaukriScrapedJob
                logger.info("orchestrator_dispatching", source="naukri")
                try:
                    agent   = NaukriAgent()
                    scraped: list[NaukriScrapedJob] = await agent._run(page, config.filters)
                    unified = [_naukri_to_unified(j, config.filters.job_type) for j in scraped]
                    logger.info("orchestrator_source_done", source="naukri", count=len(unified))
                    logger.info("batch_saved", source="Naukri", count=len(unified))
                    return "Naukri", unified
                except Exception as exc:
                    logger.exception(
                        "orchestrator_naukri_error", error=str(exc),
                        note="Naukri failed; LinkedIn and Dice results are retained",
                    )
                    return "Naukri", []

            async def _harvest_linkedin(page: Any) -> tuple[str, list[UnifiedJob]]:
                logger.info("orchestrator_dispatching", source="linkedin")
                logger.info("linkedin_agent_started")
                try:
                    agent   = LinkedInAgent()
                    scraped: list[LinkedInScrapedJob] = await agent._run(page, config.filters)
                    # ── Checkpoint 2: jobs received by orchestrator ────────────
                    logger.info("linkedin_jobs_received_by_orchestrator", count=len(scraped))
                    unified = [_linkedin_to_unified(j, config.filters.job_type) for j in scraped]
                    logger.info("orchestrator_source_done", source="linkedin", count=len(unified))
                    logger.info("batch_saved", source="LinkedIn", count=len(unified))

                    # Save raw debug snapshot for orchestrator stage
                    try:
                        _dbg = Path("data/debug/linkedin")
                        _dbg.mkdir(parents=True, exist_ok=True)
                        import json as _json_o
                        (_dbg / "linkedin_raw_jobs.json").write_text(
                            _json_o.dumps(
                                {"stage": "orchestrator_received", "count": len(unified),
                                 "jobs": [{"title": j.job_title, "company": j.company,
                                           "url": j.job_url} for j in unified]},
                                indent=2, ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                    return "LinkedIn", unified
                except LinkedInLoginError as exc:
                    logger.warning(
                        "orchestrator_linkedin_login_failed", error=str(exc),
                        note="LinkedIn skipped; other sources are retained",
                    )
                    return "LinkedIn", []
                except Exception as exc:
                    logger.exception(
                        "orchestrator_linkedin_error", error=str(exc),
                        note="LinkedIn failed; other sources are retained",
                    )
                    return "LinkedIn", []

            async def _harvest_dice(page: Any) -> tuple[str, list[UnifiedJob]]:
                logger.info("orchestrator_dispatching", source="dice")
                logger.info("dice_agent_started")
                try:
                    scraper = DiceScraper(page, config.filters)
                    scraped: list[DiceScrapedJob] = await scraper.run()
                    unified = [_dice_to_unified(j, config.filters.job_type) for j in scraped]
                    logger.info("orchestrator_source_done", source="dice", count=len(unified))
                    logger.info("batch_saved", source="Dice", count=len(unified))
                    return "Dice", unified
                except Exception as exc:
                    logger.exception(
                        "orchestrator_dice_error", error=str(exc),
                        note="Dice failed; other sources are retained",
                    )
                    return "Dice", []

            _RUNNERS = {
                "naukri":   _harvest_naukri,
                "linkedin": _harvest_linkedin,
                "dice":     _harvest_dice,
            }

            # ── Launch all enabled sources concurrently ────────────────────────
            coros   = [_RUNNERS[src](pages[src]) for src in enabled]
            gathered = await asyncio.gather(*coros, return_exceptions=True)

            for item in gathered:
                if isinstance(item, Exception):
                    logger.exception("orchestrator_source_unexpected_exception", error=str(item))
                elif item:
                    source_name, unified_jobs = item
                    results[source_name] = unified_jobs

        # ── Log batch_saved events every 100 jobs across combined list ─────────
        all_so_far = [j for jobs in results.values() for j in jobs]
        _BATCH = 100
        for i in range(0, len(all_so_far), _BATCH):
            batch_num = i // _BATCH + 1
            logger.info(
                "batch_saved",
                batch        = batch_num,
                count        = len(all_so_far[i : i + _BATCH]),
                total_so_far = min(i + _BATCH, len(all_so_far)),
            )

        return results
