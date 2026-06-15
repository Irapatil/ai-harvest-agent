"""
Naukri Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-naukri-agent          trigger a Naukri harvest run now
  POST   /naukri-setup-session      open Chrome profile for one-time manual login
  GET    /naukri-results            list all saved Naukri result files
  GET    /naukri-results/{run_id}   retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.naukri_agent import NaukriAgent, NaukriScrapedJob
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.response_models import NaukriJob, NaukriRunResponse
from app.services.config_service import ConfigService
from app.services.naukri_storage_service import NaukriStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Naukri Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = NaukriStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_naukri_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_naukri_job(j: NaukriScrapedJob) -> NaukriJob:
    return NaukriJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        source          = "Naukri",
    )


def _build_payload(
    run_id:     str,
    executed_at: str,
    f:          FiltersConfig,
    response:   NaukriRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "Naukri",
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
# POST /run-naukri-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-naukri-agent", status_code=status.HTTP_200_OK)
async def run_naukri_agent() -> Any:
    """
    Trigger a Naukri.com harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("naukri_search_started", max_jobs=f.max_jobs)

    # ── Playwright scrape via proactor thread on Windows --reload ─────────────
    async def _do_harvest() -> list[NaukriScrapedJob]:
        agent = NaukriAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[NaukriScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except RuntimeError as exc:
        err_lower = str(exc).lower()
        if "login" in err_lower or "failed" in err_lower:
            log.error("naukri_login_failed", error=str(exc))
            return _err("Naukri login failed", "naukri_login_failed")
        log.exception("naukri_harvest_error", error=str(exc))
        return _err("Naukri harvest failed", str(exc))

    except Exception as exc:
        log.exception("naukri_harvest_error", error=str(exc))
        return _err("Naukri harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("naukri_jobs_extracted", total=len(scraped))

    jobs = [_to_naukri_job(j) for j in scraped]

    # ── No results ────────────────────────────────────────────────────────────
    if not jobs:
        response = NaukriRunResponse(
            run_id      = run_id,
            status      = "no_results",
            source      = "Naukri",
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
    response = NaukriRunResponse(
        run_id      = run_id,
        status      = "success",
        source      = "Naukri",
        total_found = len(jobs),
        executed_at = now_iso,
        saved_to    = "",
        jobs        = jobs,
    )

    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("naukri_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("naukri_save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /naukri-setup-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/naukri-setup-session", status_code=status.HTTP_200_OK)
async def setup_naukri_session() -> Any:
    """
    Opens Chrome with the dedicated harvest agent profile directory.
    Log in to Naukri / recruit.naukri.com manually in the browser window that appears.
    Close the browser when done — the session is persisted in the profile
    directory and all future /run-naukri-agent calls will reuse it.

    Profile directory: data/chrome_profile (configurable in harvest_config.json)
    Times out after 10 minutes.
    """
    from app.scrapers.browser_manager import PersistentBrowserManager

    config         = _config_svc.load()
    chrome_profile = config.browser.chrome_profile

    async def _open_for_login() -> str:
        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = False,
        ) as pbm:
            page = await pbm.new_page()
            await page.goto(
                "https://recruit.naukri.com/",
                wait_until = "domcontentloaded",
                timeout    = 30_000,
            )
            logger.info(
                "naukri_setup_browser_opened",
                msg     = "Naukri login page opened. Please log in manually.",
                profile = chrome_profile,
            )

            _LOGIN_PATHS = ("/recruit/login", "/nlogin/login", "/nlogin/")
            for _ in range(300):   # 300 × 2 s = 10 min
                await page.wait_for_timeout(2_000)
                url = page.url
                if not any(p in url for p in _LOGIN_PATHS):
                    logger.info("naukri_setup_login_detected", url=url)
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
            "message":  "Naukri session saved in Chrome profile. Future /run-naukri-agent calls will reuse it.",
            "profile":  profile,
        }
    except Exception as exc:
        logger.error("naukri_setup_session_failed", error=str(exc))
        return _err("Failed to set up Naukri session", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# GET /naukri-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/naukri-results", status_code=status.HTTP_200_OK)
async def list_naukri_results() -> Any:
    """List all saved Naukri harvest run files, newest first."""
    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# GET /naukri-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/naukri-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_naukri_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved Naukri run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Naukri result found for run_id '{run_id}'",
        )
    return data
