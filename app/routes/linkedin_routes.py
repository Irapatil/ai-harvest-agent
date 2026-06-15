"""
LinkedIn Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-linkedin-agent             trigger a LinkedIn harvest run now
  POST   /linkedin-setup-session         open Chrome profile for one-time manual login
  GET    /linkedin-results               list all saved LinkedIn result files
  GET    /linkedin-results/{run_id}      retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.linkedin_agent import (
    LinkedInAgent,
    LinkedInLoginError,
    LinkedInScrapedJob,
)
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.response_models import LinkedInJob, LinkedInRunResponse
from app.services.config_service import ConfigService
from app.services.linkedin_storage_service import LinkedInStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LinkedIn Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = LinkedInStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_linkedin_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_linkedin_job(j: LinkedInScrapedJob) -> LinkedInJob:
    return LinkedInJob(
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
        company_url     = j.company_url,
        employment_type = j.employment_type,
        source          = "LinkedIn",
    )


def _build_payload(
    run_id:      str,
    executed_at: str,
    f:           FiltersConfig,
    response:    LinkedInRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "LinkedIn",
        "total_found": response.total_found,
        "filters": {
            "keyword":                  f.keyword,
            "location":                 f.location,
            "job_type":                 f.job_type,
            "work_mode":                f.work_mode,
            "search_window_hours":      f.search_window_hours,
            "max_jobs":                 f.max_jobs,
            "domain":                   f.domain,
            "hiring_entity":            f.hiring_entity,
            "gcc_mode":                 f.gcc_mode,
            "salary_min":               f.salary_min,
            "salary_max":               f.salary_max,
            "salary_currency":          f.salary_currency,
            "include_undisclosed_salary": f.include_undisclosed_salary,
        },
        "jobs": [j.model_dump() for j in response.jobs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-linkedin-agent", status_code=status.HTTP_200_OK)
async def run_linkedin_agent() -> Any:
    """
    Trigger a LinkedIn harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("linkedin_search_started", max_jobs=f.max_jobs)

    async def _do_harvest() -> list[LinkedInScrapedJob]:
        agent = LinkedInAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[LinkedInScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except LinkedInLoginError as exc:
        log.error("linkedin_login_failed", error=str(exc))
        return _err("LinkedIn login failed — check credentials or run /linkedin-save-session", str(exc))

    except Exception as exc:
        log.exception("linkedin_harvest_error", error=str(exc))
        return _err("LinkedIn harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("linkedin_jobs_extracted", total=len(scraped))

    jobs = [_to_linkedin_job(j) for j in scraped]

    # ── No results ────────────────────────────────────────────────────────────
    if not jobs:
        response = LinkedInRunResponse(
            run_id      = run_id,
            status      = "no_results",
            source      = "LinkedIn",
            total_found = 0,
            executed_at = now_iso,
            saved_to    = "",
            jobs        = [],
        )
        try:
            payload = _build_payload(run_id, now_iso, f, response)
            response.saved_to = _storage_svc.save_results(payload)
        except Exception:
            pass
        return response.model_dump()

    # ── Success ───────────────────────────────────────────────────────────────
    response = LinkedInRunResponse(
        run_id      = run_id,
        status      = "success",
        source      = "LinkedIn",
        total_found = len(jobs),
        executed_at = now_iso,
        saved_to    = "",
        jobs        = jobs,
    )

    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("linkedin_jobs_saved", count=len(jobs), saved_to=response.saved_to)
        log.info("linkedin_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("linkedin_save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /linkedin-setup-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/linkedin-setup-session", status_code=status.HTTP_200_OK)
async def setup_linkedin_session() -> Any:
    """
    Opens Chrome with the dedicated harvest agent profile directory.
    Log in to LinkedIn manually in the browser window that appears.
    Close the browser when done — the session is persisted in the profile
    directory and all future /run-linkedin-agent calls will reuse it.

    Profile directory: data/chrome_profile (configurable in harvest_config.json)
    Times out after 10 minutes.
    """
    from app.scrapers.browser_manager import PersistentBrowserManager

    config         = ConfigService().load()
    chrome_profile = config.browser.chrome_profile

    async def _open_for_login() -> str:
        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = False,
        ) as pbm:
            page = await pbm.new_page()
            await page.goto(
                "https://www.linkedin.com/login",
                wait_until = "domcontentloaded",
                timeout    = 30_000,
            )
            logger.info(
                "linkedin_setup_browser_opened",
                msg     = "LinkedIn login page opened. Please log in manually.",
                profile = chrome_profile,
            )

            _GATED = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")
            for _ in range(300):   # 300 × 2 s = 10 min
                await page.wait_for_timeout(2_000)
                url = page.url
                if "linkedin.com" in url and not any(p in url for p in _GATED):
                    logger.info("linkedin_setup_login_detected", url=url)
                    break
            else:
                raise RuntimeError("Setup timed out — login not completed within 10 minutes")

        return chrome_profile

    try:
        if needs_proactor():
            profile: str = await run_in_proactor(_open_for_login)
        else:
            profile = await _open_for_login()
        return {
            "status":   "ready",
            "message":  "LinkedIn session saved in Chrome profile. Future /run-linkedin-agent calls will reuse it.",
            "profile":  profile,
        }
    except Exception as exc:
        logger.error("linkedin_setup_session_failed", error=str(exc))
        return _err("Failed to set up LinkedIn session", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results", status_code=status.HTTP_200_OK)
async def list_linkedin_results() -> Any:
    """List all saved LinkedIn harvest run files, newest first."""
    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_linkedin_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved LinkedIn run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No LinkedIn result found for run_id '{run_id}'",
        )
    return data
