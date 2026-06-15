"""
Dice.com Harvest Agent — delegates scraping to DiceScraper.

Uses a persistent Chrome profile for session reuse.  No login automation.
"""
from __future__ import annotations

import structlog
from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import PersistentBrowserManager
from app.scrapers.dice_scraper import DiceScrapedJob, DiceScraper

logger = structlog.get_logger(__name__)


class DiceAgent:
    """
    Dice.com job harvester using a persistent Chrome profile session.

    Dice is a public job board — most searches work without login.
    The persistent profile is still used for consistency with other agents.
    """

    def __init__(self) -> None:
        pass

    # ── Public API ─────────────────────────────────────────────────────────────

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[DiceScrapedJob]:
        """
        Open Dice.com with the persistent Chrome profile and harvest jobs.
        Returns list[DiceScrapedJob]. Never raises — errors are logged and [] returned.
        """
        from app.services.config_service import ConfigService
        chrome_profile = ConfigService().load().browser.chrome_profile

        logger.info(
            "config_loaded",
            source              = "dice",
            keyword             = filters.keyword,
            location            = filters.location,
            job_type            = filters.job_type,
            work_mode           = filters.work_mode,
            domain              = getattr(filters, "domain", "Any"),
            search_window_hours = filters.search_window_hours,
            max_jobs            = filters.max_jobs,
            chrome_profile      = chrome_profile,
        )
        logger.info(
            "dice_agent_started",
            keyword        = filters.keyword,
            location       = filters.location,
            job_type       = filters.job_type,
            work_mode      = filters.work_mode,
            max_jobs       = filters.max_jobs,
            chrome_profile = chrome_profile,
        )
        try:
            async with PersistentBrowserManager(
                profile_dir = chrome_profile,
                headless    = headless,
                slow_mo     = slow_mo,
            ) as pbm:
                page    = await pbm.new_page()
                scraper = DiceScraper(page, filters)
                jobs    = await scraper.run()
            logger.info("dice_harvest_complete", total=len(jobs))
            logger.info("agent_completed", source="dice", total=len(jobs))
            return jobs
        except Exception as exc:
            logger.exception("agent_failed", source="dice", error=str(exc))
            return []

