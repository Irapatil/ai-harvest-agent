"""
Verification Agent — validates job listings against company career pages.

For each job, the agent:
  1. Derives candidate career-page URLs from the company name.
  2. Opens the URL via a shared Playwright browser (headless).
  3. Searches the rendered page for key words from the job title.
  4. Sets verification_status to one of:
       "verified"              — career page found AND job title matched
       "not_verified"          — career page found but job title not matched
       "career_page_not_found" — no reachable career page found
       "skipped"               — verification.enabled is False
"""
from __future__ import annotations

import asyncio
import re

import structlog
from playwright.async_api import Browser, Page, async_playwright

from app.models.harvest_models import VerificationConfig
from app.models.unified_job import UnifiedJob
from app.scrapers.browser_manager import _LAUNCH_ARGS, _USER_AGENT

logger = structlog.get_logger(__name__)

_CAREER_SUFFIXES = ["/careers", "/jobs", "/work-with-us", "/join-us", "/opportunities"]
_MAX_CONCURRENT  = 3     # parallel verification tabs
_NAV_TIMEOUT_MS  = 12_000


def _company_slug(company: str) -> str:
    """'Tiger Analytics India' → 'tigeranalyticsindia' (max 30 chars)."""
    return re.sub(r"[^a-z0-9]", "", company.lower())[:30]


def _candidate_urls(company: str) -> list[str]:
    slug = _company_slug(company)
    if not slug:
        return []
    base = f"https://www.{slug}.com"
    return [f"{base}{suffix}" for suffix in _CAREER_SUFFIXES]


class VerificationAgent:
    """
    Verify job listings against company career pages using Playwright.

    Parameters
    ──────────
    cfg      VerificationConfig from harvest_config.
    headless  Run verification browser headless (default True).
    """

    def __init__(self, cfg: VerificationConfig, headless: bool = True) -> None:
        self._cfg      = cfg
        self._headless = headless

    async def verify_batch(self, jobs: list[UnifiedJob]) -> list[UnifiedJob]:
        """
        Verify every job in *jobs*.
        Marks jobs with verification_status = "skipped" when disabled.
        Returns the same list (mutated in-place) with status populated.
        """
        if not self._cfg.enabled:
            for j in jobs:
                j.verification_status = "skipped"
            return jobs

        logger.info("verification_batch_start", total=len(jobs))

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless, args=_LAUNCH_ARGS)
            sem     = asyncio.Semaphore(_MAX_CONCURRENT)

            async def _one(job: UnifiedJob) -> None:
                async with sem:
                    try:
                        ctx  = await browser.new_context(user_agent=_USER_AGENT)
                        page = await ctx.new_page()
                        job.verification_status = await self._check(page, job)
                        await ctx.close()
                    except Exception as exc:
                        logger.debug(
                            "verification_tab_error",
                            company=job.company,
                            error=str(exc),
                        )
                        job.verification_status = "career_page_not_found"

            await asyncio.gather(*[_one(j) for j in jobs])
            await browser.close()

        counts = {
            "verified":              sum(1 for j in jobs if j.verification_status == "verified"),
            "not_verified":          sum(1 for j in jobs if j.verification_status == "not_verified"),
            "career_page_not_found": sum(1 for j in jobs if j.verification_status == "career_page_not_found"),
        }
        logger.info("verification_batch_done", total=len(jobs), **counts)
        return jobs

    async def _check(self, page: Page, job: UnifiedJob) -> str:
        for url in _candidate_urls(job.company):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                if resp and resp.status < 400:
                    body       = (await page.content()).lower()
                    title_kws  = [w for w in job.job_title.lower().split() if len(w) > 3]
                    hits       = sum(1 for w in title_kws if w in body)
                    threshold  = max(1, len(title_kws) // 2)
                    status     = "verified" if hits >= threshold else "not_verified"
                    logger.debug(
                        "verification_result",
                        company=job.company,
                        url=url,
                        status=status,
                        hits=hits,
                    )
                    return status
            except Exception:
                continue
        return "career_page_not_found"
