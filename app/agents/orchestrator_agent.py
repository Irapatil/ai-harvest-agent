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

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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

# Fixed execution priority — lower index = runs first
_SOURCE_PRIORITY = ["naukri", "linkedin", "dice"]


# ── Converters: source-specific dataclass → UnifiedJob ────────────────────────

def _naukri_to_unified(j: "NaukriScrapedJob", job_type: str) -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        work_mode       = j.work_mode,
        source          = "Naukri",
        job_type        = job_type,
    )


def _linkedin_to_unified(j: LinkedInScrapedJob, job_type: str) -> UnifiedJob:
    return UnifiedJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        work_mode       = j.work_mode,
        source          = "LinkedIn",
        job_type        = job_type,
    )


def _dice_to_unified(j: "DiceScrapedJob", job_type: str) -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        work_mode       = j.work_mode,
        source          = "Dice",
        job_type        = job_type,
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
        before_dedup = len(all_unified)
        all_unified  = _deduplicate(all_unified)
        removed      = before_dedup - len(all_unified)
        logger.info(
            "deduplication_completed",
            before  = before_dedup,
            after   = len(all_unified),
            removed = removed,
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
        result.completed_at  = datetime.now(timezone.utc)

        elapsed = (result.completed_at - started_at).total_seconds()
        logger.info(
            "harvest_completed",
            run_id   = run_id,
            sources  = result.sources_executed,
            total    = result.total_jobs,
            verified = result.verified_jobs,
            elapsed  = round(elapsed, 1),
            path     = combined_path,
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
        Run each enabled source agent in priority order.
        Returns {source_name: [UnifiedJob, …]}.
        """
        results: dict[str, list[UnifiedJob]] = {}

        for source in _SOURCE_PRIORITY:
            if source == "naukri" and config.sources.naukri:
                from app.agents.naukri_agent import NaukriAgent, NaukriScrapedJob
                logger.info("orchestrator_dispatching", source="naukri")
                agent  = NaukriAgent()
                scraped: list[NaukriScrapedJob] = await agent.harvest(
                    filters  = config.filters,
                    headless = config.browser.headless,
                    slow_mo  = config.browser.slow_mo_ms,
                )
                results["Naukri"] = [_naukri_to_unified(j, config.filters.job_type) for j in scraped]
                logger.info("orchestrator_source_done", source="naukri", count=len(scraped))

            elif source == "linkedin" and config.sources.linkedin:
                logger.info("orchestrator_dispatching", source="linkedin")
                logger.info("linkedin_agent_started")
                try:
                    agent_li = LinkedInAgent()
                    li_scraped: list[LinkedInScrapedJob] = await agent_li.harvest(
                        filters  = config.filters,
                        headless = config.browser.headless,
                        slow_mo  = config.browser.slow_mo_ms,
                    )
                    results["LinkedIn"] = [_linkedin_to_unified(j, config.filters.job_type) for j in li_scraped]
                    logger.info("orchestrator_source_done", source="linkedin", count=len(li_scraped))
                except LinkedInLoginError as exc:
                    logger.warning(
                        "orchestrator_linkedin_login_failed",
                        error=str(exc),
                        note="LinkedIn skipped; results from other sources are retained",
                    )
                    results["LinkedIn"] = []
                except Exception as exc:
                    logger.exception(
                        "orchestrator_linkedin_error",
                        error=str(exc),
                        note="LinkedIn failed with unexpected error; results from other sources are retained",
                    )
                    results["LinkedIn"] = []

            elif source == "dice" and config.sources.dice:
                from app.agents.dice_agent import DiceAgent, DiceScrapedJob
                logger.info("orchestrator_dispatching", source="dice")
                logger.info("dice_agent_started")
                try:
                    agent_dice   = DiceAgent()
                    dice_scraped: list[DiceScrapedJob] = await agent_dice.harvest(
                        filters  = config.filters,
                        headless = config.browser.headless,
                        slow_mo  = config.browser.slow_mo_ms,
                    )
                    results["Dice"] = [_dice_to_unified(j, config.filters.job_type) for j in dice_scraped]
                    logger.info("orchestrator_source_done", source="dice", count=len(dice_scraped))
                except Exception as exc:
                    logger.exception(
                        "orchestrator_dice_error",
                        error = str(exc),
                        note  = "Dice failed; results from other sources are retained",
                    )
                    results["Dice"] = []

        if not results:
            logger.warning("orchestrator_no_sources_enabled")

        return results
