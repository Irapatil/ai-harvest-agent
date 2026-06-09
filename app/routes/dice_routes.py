"""
Dice Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-dice-agent           trigger a Dice harvest run now
  GET    /dice-results             list all saved Dice result files
  GET    /dice-results/{run_id}    retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.dice_agent import DiceAgent
from app.scrapers.dice_scraper import DiceScrapedJob
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.response_models import DiceJob, DiceRunResponse
from app.services.config_service import ConfigService
from app.services.dice_storage_service import DiceStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Dice Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = DiceStorageService()


def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_dice_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_dice_job(j: DiceScrapedJob) -> DiceJob:
    return DiceJob(
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
        employment_type = j.employment_type,
        source          = "Dice",
    )


def _build_payload(
    run_id:      str,
    executed_at: str,
    f:           FiltersConfig,
    response:    DiceRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "Dice",
        "total_found": response.total_found,
        "filters": {
            "keyword":                    f.keyword,
            "location":                   f.location,
            "job_type":                   f.job_type,
            "work_mode":                  f.work_mode,
            "search_window_hours":        f.search_window_hours,
            "max_jobs":                   f.max_jobs,
            "domain":                     f.domain,
            "hiring_entity":              f.hiring_entity,
            "gcc_mode":                   f.gcc_mode,
            "salary_min":                 f.salary_min,
            "salary_max":                 f.salary_max,
            "salary_currency":            f.salary_currency,
            "include_undisclosed_salary": f.include_undisclosed_salary,
        },
        "jobs": [j.model_dump() for j in response.jobs],
    }


@router.post("/run-dice-agent", status_code=status.HTTP_200_OK)
async def run_dice_agent() -> Any:
    """
    Trigger a Dice.com harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("dice_search_started", max_jobs=f.max_jobs)

    async def _do_harvest() -> list[DiceScrapedJob]:
        agent = DiceAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[DiceScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except Exception as exc:
        log.exception("dice_harvest_error", error=str(exc))
        return _err("Dice harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("dice_jobs_extracted", total=len(scraped))

    jobs = [_to_dice_job(j) for j in scraped]

    if not jobs:
        response = DiceRunResponse(
            run_id=run_id, status="no_results", source="Dice",
            total_found=0, executed_at=now_iso, saved_to="", jobs=[],
        )
        try:
            payload = _build_payload(run_id, now_iso, f, response)
            response.saved_to = _storage_svc.save_results(payload)
        except Exception:
            pass
        return response.model_dump()

    response = DiceRunResponse(
        run_id=run_id, status="success", source="Dice",
        total_found=len(jobs), executed_at=now_iso, saved_to="", jobs=jobs,
    )
    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("dice_jobs_saved", count=len(jobs), saved_to=response.saved_to)
        log.info("dice_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("dice_save_failed", error=str(exc))

    return response.model_dump()


@router.get("/dice-results", status_code=status.HTTP_200_OK)
async def list_dice_results() -> Any:
    """List all saved Dice harvest run files, newest first."""
    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


@router.get("/dice-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_dice_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved Dice run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Dice result found for run_id '{run_id}'",
        )
    return data
