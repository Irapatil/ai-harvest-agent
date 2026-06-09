"""
LinkedIn → Gemini enrichment pipeline.

Three-phase orchestration
─────────────────────────
  Phase 1  LinkedInScraper.search()            → list[LinkedInJobCard]
  Phase 2  LinkedInScraper.fetch_description() → raw description text per job
  Phase 3  GeminiService.parse_job_description() → ParsedJobDescription per job

Phases 2 + 3 run concurrently (bounded by config.description_concurrency) so a
batch of 25 jobs completes in roughly the time of the slowest few, not the sum.

Smart merge
───────────
After Phase 3, `_merge` reconciles card-level data (from LinkedIn's search
results) with Gemini's extracted data:

  effective_location    Gemini location  OR  card location
  effective_work_mode   Gemini work_mode (card has no work_mode)
  effective_salary      Gemini salary    (card has no salary)
  effective_skills      Gemini skills    (card has no skills)

The merge never discards card data — it fills Gemini's gaps.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

import structlog

from app.config import Settings
from app.models.linkedin import (
    EnrichedLinkedInJob,
    LinkedInHarvestResult,
    LinkedInSearchConfig,
    LinkedInSearchResult,
)
from app.models.job_parser import EmploymentType, WorkMode
from app.scrapers.linkedin_scraper import LinkedInJobCard, LinkedInScraper
from app.services.gemini_service import GeminiService

logger = structlog.get_logger(__name__)

# Minimum description length Gemini needs for useful extraction.
_MIN_DESC_CHARS = 50


class LinkedInPipelineService:
    """
    Orchestrates the full LinkedIn harvest pipeline.

    GeminiService is injected (not constructed here) so it can be:
      • the shared cached singleton from get_gemini() in normal operation
      • a mock in tests
      • None in search-only mode (no Gemini call is ever made)
    """

    def __init__(
        self,
        settings: Settings,
        gemini: GeminiService | None = None,
    ) -> None:
        self._settings = settings
        self._gemini   = gemini          # None → Gemini disabled

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    async def harvest(self, config: LinkedInSearchConfig) -> LinkedInHarvestResult:
        """
        Run all three phases and return fully enriched jobs.

        Flow
        ────
        1. Playwright search → collect LinkedInJobCards
        2. Playwright detail pages → raw description text (parallel, bounded)
        3. Gemini parse → ParsedJobDescription (parallel, same semaphore)
        4. Smart-merge card + Gemini → EnrichedLinkedInJob
        """
        t0     = time.perf_counter()
        errors: list[str] = []

        async with LinkedInScraper(config) as scraper:
            # ── Phase 1: Playwright search ────────────────────────────────────
            raw_cards = await scraper.search(config)
            logger.info("pipeline_phase1_done", count=len(raw_cards), keywords=config.keywords)

            # ── Phases 2 + 3: per-job concurrent processing ───────────────────
            sem = asyncio.Semaphore(config.description_concurrency)

            async def _process(card: LinkedInJobCard) -> EnrichedLinkedInJob:
                async with sem:
                    return await self._process_one(card, config, scraper, errors)

            enriched: list[EnrichedLinkedInJob] = list(
                await asyncio.gather(*[_process(c) for c in raw_cards])
            )

        duration_ms = round((time.perf_counter() - t0) * 1_000, 1)

        result = LinkedInHarvestResult(
            jobs           = enriched,
            search_config  = config,
            total_found    = len(raw_cards),
            total_described= sum(1 for j in enriched if j.raw_description),
            total_parsed   = sum(1 for j in enriched if j.parsed is not None),
            duration_ms    = duration_ms,
            errors         = errors,
        )

        logger.info(
            "pipeline_complete",
            found       = result.total_found,
            described   = result.total_described,
            parsed      = result.total_parsed,
            duration_ms = duration_ms,
            errors      = len(errors),
        )
        return result

    async def search_only(self, config: LinkedInSearchConfig) -> LinkedInSearchResult:
        """
        Phase 1 only — returns raw LinkedIn card data.

        No detail pages, no Gemini — fast and free.  Use it to preview
        results before committing to a full harvest.
        """
        t0 = time.perf_counter()

        lightweight = config.model_copy(
            update={"fetch_descriptions": False, "parse_with_gemini": False}
        )
        async with LinkedInScraper(lightweight) as scraper:
            cards = await scraper.search(lightweight)

        duration_ms = round((time.perf_counter() - t0) * 1_000, 1)
        return LinkedInSearchResult(
            jobs          = [asdict(c) for c in cards],
            keywords      = config.keywords,
            total_found   = len(cards),
            pages_scraped = min(config.max_search_pages, max(1, -(-len(cards) // 25))),
            duration_ms   = duration_ms,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Internal — single-job processing
    # ══════════════════════════════════════════════════════════════════════════

    async def _process_one(
        self,
        card:   LinkedInJobCard,
        config: LinkedInSearchConfig,
        scraper: LinkedInScraper,
        errors: list[str],
    ) -> EnrichedLinkedInJob:
        """
        Phase 2 + Phase 3 for a single job card.

        Phase 2 — fetch description
        ───────────────────────────
        Navigate to card.job_url and extract the full description text.
        Skipped if fetch_descriptions=False or job_url is blank.

        Phase 3 — Gemini parse
        ──────────────────────
        Send the raw description to GeminiService and get a ParsedJobDescription.
        Skipped if parse_with_gemini=False, no description, or GeminiService is None.

        Smart merge (phase 4)
        ──────────────────────
        The returned EnrichedLinkedInJob carries *both* the raw card data and
        Gemini's parse.  effective_* properties on the model do the merge at
        read time:
          - effective_location  → Gemini.location  or card.location
          - effective_work_mode → Gemini.work_mode (card has none)
          - effective_salary    → Gemini.salary    (card has none)
          - effective_skills    → Gemini.skills    (card has none)
        """
        raw_desc:   str | None = None
        desc_error: str | None = None
        parsed_result          = None
        parse_error: str | None = None

        # ── Phase 2: description fetch ────────────────────────────────────────
        if config.fetch_descriptions and card.job_url:
            try:
                raw_desc = await scraper.fetch_description(card.job_url)
                logger.debug(
                    "description_fetched",
                    job_id = card.job_id,
                    chars  = len(raw_desc) if raw_desc else 0,
                )
            except Exception as exc:
                desc_error = str(exc)
                errors.append(f"[fetch] {card.job_id}: {exc}")
                logger.warning("description_fetch_failed", job_id=card.job_id, error=str(exc))

        # ── Phase 3: Gemini parse ─────────────────────────────────────────────
        gemini_eligible = (
            config.parse_with_gemini
            and self._gemini is not None
            and raw_desc is not None
            and len(raw_desc.strip()) >= _MIN_DESC_CHARS
        )
        if gemini_eligible:
            try:
                assert self._gemini is not None   # mypy narrowing
                response      = await self._gemini.parse_job_description(raw_desc)  # type: ignore[arg-type]
                parsed_result = _backfill_from_card(response.parsed, card)

                logger.debug(
                    "gemini_parsed",
                    job_id     = card.job_id,
                    confidence = parsed_result.confidence_score,
                    skills     = len(parsed_result.skills.required),
                )
            except Exception as exc:
                parse_error = str(exc)
                errors.append(f"[parse] {card.job_id}: {exc}")
                logger.warning("gemini_parse_failed", job_id=card.job_id, error=str(exc))

        return EnrichedLinkedInJob(
            job_id                  = card.job_id,
            job_title               = card.job_title,
            company                 = card.company,
            location                = card.location,     # raw card location kept
            job_url                 = card.job_url,
            posted_time             = card.posted_time,
            raw_description         = raw_desc,
            description_length      = len(raw_desc) if raw_desc else 0,
            description_fetch_error = desc_error,
            parsed                  = parsed_result,
            parse_error             = parse_error,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Smart merge helper
# ══════════════════════════════════════════════════════════════════════════════

def _backfill_from_card(
    parsed: "ParsedJobDescription",  # type: ignore[name-defined]
    card: LinkedInJobCard,
) -> "ParsedJobDescription":
    """
    Fill Gemini gaps with data already present on the LinkedIn card.

    Rules
    ─────
    • location      — use card's location when Gemini found nothing
    • job_title     — use card's title when Gemini found nothing
    • company_name  — use card's company when Gemini found nothing
    • work_mode     — NOT backfilled (card doesn't carry this)
    • salary        — NOT backfilled (card doesn't carry this)

    Returns a new ParsedJobDescription (Pydantic models are immutable by default).
    """
    updates: dict = {}

    if not parsed.location and card.location:
        updates["location"] = card.location

    if not parsed.job_title and card.job_title:
        updates["job_title"] = card.job_title

    if not parsed.company_name and card.company:
        updates["company_name"] = card.company

    if not updates:
        return parsed          # nothing to backfill — return as-is

    return parsed.model_copy(update=updates)
