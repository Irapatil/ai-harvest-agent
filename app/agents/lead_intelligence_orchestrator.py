"""
Lead Intelligence Orchestrator — coordinates the full hybrid LinkedIn + Premium Naukri flow.

Pipeline
────────

Rule Engine (LeadIntelligenceRequest)
         ↓
LinkedIn Lead Agent
    Search LinkedIn POSTS for hiring keyword
    Extract recruiter + post metadata
         ↓
For each recruiter:
    Contact Validator — does this post have email AND phone?
         ↓  YES             ↓  NO
         ↓                  Premium Naukri Agent
         ↓                  Search recruiter profile by name + company
         ↓                  Extract email / phone / employment history
         ↓                 ↓
Lead Merge Agent
    Merge LinkedIn + Naukri data
    Deduplicate by LinkedIn URL / name+company
         ↓
Confidence Validator
    Validate email / phone / LinkedIn URL format
    Assign confidence score (High / Medium / Low)
    Apply minimum_confidence filter
         ↓
Save JSON + Excel
         ↓
Return LeadIntelligenceResult (CRM-ready dataset)

Security contract
─────────────────
• Uses the persistent Chrome profile — user logged in manually.
• NO login automation.
• NO fabricated data.
• Only scraped, publicly visible contact data is stored.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.agents.confidence_validator import ConfidenceValidator
from app.agents.lead_merge_agent import LeadMergeAgent
from app.agents.linkedin_lead_agent import LinkedInLeadAgent
from app.agents.premium_naukri_agent import PremiumNaukriAgent
from app.models.lead_models import (
    LeadIntelligenceRequest,
    LeadIntelligenceResult,
    LeadRecord,
    LinkedInPost,
)
from app.scrapers.browser_manager import PersistentBrowserManager
from app.services.lead_config_service import LeadConfigService

logger = structlog.get_logger(__name__)

_OUTPUT_DIR = Path("data/results/lead_intelligence")


def _make_run_id() -> str:
    return "li_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _has_contact(record: LeadRecord) -> bool:
    """True when at least email OR phone is present after LinkedIn extraction."""
    return bool(record.official_email or record.contact_number)


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class LeadIntelligenceOrchestrator:
    """
    Coordinates the full Hybrid Lead Intelligence pipeline.

    Usage::

        from app.models.lead_models import LeadIntelligenceRequest
        from app.agents.lead_intelligence_orchestrator import LeadIntelligenceOrchestrator

        req    = LeadIntelligenceRequest(keyword="AI Engineer", max_leads=50)
        result = await LeadIntelligenceOrchestrator().run(req)
    """

    def __init__(self) -> None:
        self._config_svc  = LeadConfigService()
        self._merger      = LeadMergeAgent()
        self._validator   = ConfidenceValidator()

    async def run(self, request: LeadIntelligenceRequest) -> LeadIntelligenceResult:
        """Execute the full pipeline and return a LeadIntelligenceResult."""
        cfg     = self._config_svc.load()
        run_id  = _make_run_id()
        result  = LeadIntelligenceResult()
        result.run_id      = run_id
        result.executed_at = datetime.now(timezone.utc).isoformat()
        result.keyword     = request.keyword
        result.started_at  = datetime.now(timezone.utc)

        # Override config with request params
        fallback_enabled    = request.fallback_to_premium_naukri and cfg.get(
            "fallback_to_premium_naukri", True
        )
        min_confidence      = request.minimum_confidence
        li_cfg              = cfg.get("linkedin", {})
        naukri_cfg          = cfg.get("premium_naukri", {})
        max_posts           = min(li_cfg.get("max_posts", 50), request.max_leads)

        logger.info(
            "lead_intelligence_started",
            run_id              = run_id,
            keyword             = request.keyword,
            max_leads           = request.max_leads,
            sources             = request.search_sources,
            fallback_enabled    = fallback_enabled,
            min_confidence      = min_confidence,
        )

        # ── Open a shared persistent browser session ─────────────────────────
        chrome_profile = cfg.get("browser", {}).get("chrome_profile", "data/chrome_profile")

        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = cfg.get("browser", {}).get("headless", True),
            slow_mo     = cfg.get("browser", {}).get("slow_mo_ms", 0),
        ) as pbm:
            linkedin_page = await pbm.new_page()
            naukri_page   = await pbm.new_page()  # separate tab for Naukri

            # ── Step 1: LinkedIn post search ─────────────────────────────────
            linkedin_posts: list[LinkedInPost] = []

            if "linkedin" in request.search_sources:
                linkedin_agent = LinkedInLeadAgent(
                    max_posts    = max_posts,
                    max_pages    = li_cfg.get("max_pages", 5),
                    scroll_times = li_cfg.get("scroll_times", 8),
                )
                try:
                    linkedin_posts = await linkedin_agent.search_posts(
                        page    = linkedin_page,
                        keyword = request.keyword,
                    )
                except Exception as exc:
                    logger.exception("linkedin_agent_error", error=str(exc))

            result.linkedin_posts_found = len(linkedin_posts)
            logger.info(
                "linkedin_agent_completed",
                posts_found = len(linkedin_posts),
                with_email  = sum(1 for p in linkedin_posts if p.raw_email),
                with_phone  = sum(1 for p in linkedin_posts if p.raw_phone),
            )

            # ── Step 2: Build initial LeadRecords from LinkedIn posts ────────
            initial_records: list[LeadRecord] = []
            for post in linkedin_posts:
                if not post.author_name:
                    continue
                record = self._merger.build_from_linkedin(post)
                initial_records.append(record)
                if post.author_name:
                    logger.info("recruiter_found", name=post.author_name, company=record.company)
                if record.official_email or record.contact_number:
                    logger.info(
                        "contact_found",
                        source    = "LinkedIn",
                        recruiter = post.author_name,
                        email     = record.official_email,
                        phone     = record.contact_number,
                    )

            result.recruiters_extracted = len(initial_records)

            # ── Step 3: Premium Naukri fallback for leads without contact ────
            if fallback_enabled and "premium_naukri" in request.search_sources:
                naukri_agent = PremiumNaukriAgent(
                    max_profiles = naukri_cfg.get("max_profiles", 3)
                )

                no_contact = [r for r in initial_records if not _has_contact(r)]
                logger.info(
                    "premium_naukri_fallback_batch",
                    total_leads  = len(initial_records),
                    need_fallback = len(no_contact),
                )

                for record in no_contact:
                    if not record.recruiter_name:
                        continue
                    try:
                        naukri_profile = await naukri_agent.search_recruiter(
                            page            = naukri_page,
                            recruiter_name  = record.recruiter_name,
                            current_company = record.current_company or record.company,
                        )
                        if naukri_profile:
                            result.premium_naukri_fallbacks += 1
                            self._merger.enrich_with_naukri(
                                record,
                                naukri_profile,
                                company = record.current_company or record.company,
                            )
                    except Exception as exc:
                        logger.warning(
                            "premium_naukri_record_error",
                            recruiter = record.recruiter_name,
                            error     = str(exc),
                        )

            # ── Close browser ─────────────────────────────────────────────────
            # (context manager handles cleanup)

        # ── Step 4: Deduplication ────────────────────────────────────────────
        merged_records = self._merger.deduplicate(initial_records)
        logger.info("deduplication_completed", unique_leads=len(merged_records))

        # ── Step 5: Confidence validation + filtering ────────────────────────
        validated = self._validator.validate_batch(merged_records, min_confidence)

        result.leads              = validated[:request.max_leads]
        result.total_leads        = len(result.leads)
        result.high_confidence    = sum(1 for r in result.leads if r.confidence_score == "High")
        result.medium_confidence  = sum(1 for r in result.leads if r.confidence_score == "Medium")
        result.low_confidence     = sum(1 for r in result.leads if r.confidence_score == "Low")
        result.sources_used       = list(request.search_sources)
        result.completed_at       = datetime.now(timezone.utc)

        # ── Step 6: Save JSON ────────────────────────────────────────────────
        try:
            json_path = self._save_json(result, run_id)
            result.json_path = json_path
            logger.info("json_saved", path=json_path, total=result.total_leads)
        except Exception as exc:
            logger.warning("json_save_failed", error=str(exc))

        # ── Step 7: Save Excel ───────────────────────────────────────────────
        try:
            from app.services.lead_excel_service import LeadExcelService
            excel_path = LeadExcelService().export(result, run_id)
            result.excel_path = excel_path
            logger.info("excel_saved", path=excel_path, total=result.total_leads)
        except Exception as exc:
            logger.warning("excel_export_failed", error=str(exc))

        logger.info(
            "lead_intelligence_completed",
            run_id                   = run_id,
            total_leads              = result.total_leads,
            high                     = result.high_confidence,
            medium                   = result.medium_confidence,
            low                      = result.low_confidence,
            premium_naukri_fallbacks = result.premium_naukri_fallbacks,
            runtime_minutes          = result.runtime_minutes,
        )
        return result

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save_json(self, result: LeadIntelligenceResult, run_id: str) -> str:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = _OUTPUT_DIR / f"{run_id}_{ts}_leads.json"

        payload = {
            "run_id":                   result.run_id,
            "executed_at":              result.executed_at,
            "keyword":                  result.keyword,
            "summary": {
                "total_leads":              result.total_leads,
                "high_confidence":          result.high_confidence,
                "medium_confidence":        result.medium_confidence,
                "low_confidence":           result.low_confidence,
                "sources_used":             result.sources_used,
                "linkedin_posts_found":     result.linkedin_posts_found,
                "recruiters_extracted":     result.recruiters_extracted,
                "premium_naukri_fallbacks": result.premium_naukri_fallbacks,
                "runtime_minutes":          result.runtime_minutes,
            },
            "leads": [lead.to_output_dict() for lead in result.leads],
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(path.resolve())
