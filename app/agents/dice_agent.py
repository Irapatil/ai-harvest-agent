"""
Dice.com Harvest Agent — delegates all browser work to DiceScraper.
No authentication required: Dice.com job search is public.
"""
from __future__ import annotations

import structlog

from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import BrowserManager
from app.scrapers.dice_scraper import DiceScraper, DiceScrapedJob

logger = structlog.get_logger(__name__)


class DiceAgent:
    """
    Autonomous Dice.com job harvester.
    Instantiate fresh for each run — BrowserManager is created and destroyed inside harvest().
    """

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[DiceScrapedJob]:
        """
        Open Dice.com and harvest jobs matching filters.
        Returns list[DiceScrapedJob]. Never raises — errors are logged and [] returned.
        """
        logger.info(
            "dice_agent_started",
            keyword   = filters.keyword,
            location  = filters.location,
            job_type  = filters.job_type,
            work_mode = filters.work_mode,
            max_jobs  = filters.max_jobs,
        )
        try:
            async with BrowserManager(headless=headless, slow_mo=slow_mo) as bm:
                page    = await bm.new_page()
                scraper = DiceScraper(page, filters)
                jobs    = await scraper.run()
            logger.info("dice_harvest_complete", total=len(jobs))
            return jobs
        except Exception as exc:
            logger.exception("dice_harvest_error", error=str(exc))
            return []
