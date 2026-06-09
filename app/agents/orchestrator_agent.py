"""
Orchestrator Agent — unified entry point for every harvest execution.

Two public methods
──────────────────
run()       Legacy interface — used by /run-harvest and the scheduler.
            Returns (list[HarvestJob], source_label_str).

run_all()   Full pipeline — used by POST /run-harvest-agent.
            Executes sources in priority order, applies business filters,
            runs optional verification, returns OrchestratorResult.

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

from dataclasses import dataclass, field
from datetime import datetime, timezone

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

logger = structlog.get_logger(__name__)

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
        run optional company verification, and return OrchestratorResult.
        """
        config     = self._config
        started_at = datetime.now(timezone.utc)
        result     = OrchestratorResult(started_at=started_at)

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

        logger.info("orchestrator_filtered_total", total=len(all_unified))

        # ── Step 4: company verification (optional) ───────────────────────────
        if config.filters.verification.enabled:
            from app.agents.verification_agent import VerificationAgent
            verifier    = VerificationAgent(config.filters.verification, headless=True)
            all_unified = await verifier.verify_batch(all_unified)

        result.all_jobs      = all_unified
        result.completed_at  = datetime.now(timezone.utc)

        logger.info(
            "orchestrator_run_all_complete",
            sources   = result.sources_executed,
            total     = result.total_jobs,
            verified  = result.verified_jobs,
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
