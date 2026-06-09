"""
Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-harvest              trigger a harvest run now
  GET    /harvest-results          list all saved result files
  GET    /harvest-results/{run_id} retrieve one saved result
  GET    /harvest-config           get the current configuration
  PUT    /harvest-config           update configuration and re-apply schedule
  GET    /harvest-schedule/status  get scheduler status
  POST   /harvest-schedule/toggle  enable / disable the scheduler
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from app.agents.orchestrator_agent import OrchestratorAgent
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import HarvestConfig
from app.models.response_models import (
    HarvestFiltersSnapshot,
    HarvestJob,
    HarvestRunResponse,
    ResultFileSummary,
    ResultsListResponse,
    ScheduleStatusResponse,
)
from app.services.config_service import ConfigService
from app.services.harvest_storage_service import HarvestStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = HarvestStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_{slug[:40]}"


def _err(msg: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=200, content=body)


def _get_scheduler(request: Request):
    """Retrieve the SchedulerService instance stored on app.state."""
    return getattr(request.app.state, "scheduler", None)


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-harvest", status_code=status.HTTP_200_OK)
async def run_harvest() -> Any:
    """
    Trigger a harvest run using the current harvest_config.json settings.
    Edit filters via PUT /harvest-config or the Rule Engine UI.
    """
    config = _config_svc.load()

    if not config.sources.linkedin:
        return _err("No sources enabled", "Set sources.linkedin = true in harvest_config.json")

    run_id  = _make_run_id(config.filters.keyword, config.filters.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(
        run_id   = run_id,
        keyword  = config.filters.keyword,
        location = config.filters.location,
    )
    log.info("harvest_run_start")

    # ── Orchestrate ───────────────────────────────────────────────────────────
    # On Windows with --reload, uvicorn forces SelectorEventLoop which cannot
    # spawn subprocesses.  run_in_proactor() moves Playwright into a thread
    # with its own ProactorEventLoop so Chromium can launch normally.
    try:
        orch = OrchestratorAgent(config)
        if needs_proactor():
            log.debug("using_proactor_thread")
            jobs, src_label = await run_in_proactor(orch.run)
        else:
            jobs, src_label = await orch.run()
    except Exception as exc:
        log.exception("harvest_run_error", error=str(exc))
        return _err("Harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("harvest_run_complete", total=len(jobs))

    # ── Snapshot of active filters ────────────────────────────────────────────
    f = config.filters
    filters_snap = HarvestFiltersSnapshot(
        keyword             = f.keyword,
        location            = f.location,
        job_type            = f.job_type,
        work_mode           = f.work_mode,
        search_window_hours = f.search_window_hours,
        max_jobs            = f.max_jobs,
    )

    # ── Build response ────────────────────────────────────────────────────────
    response = HarvestRunResponse(
        status      = "success" if jobs else "no_results",
        run_id      = run_id,
        executed_at = now_iso,
        total_jobs  = len(jobs),
        saved_to    = "",
        source      = src_label,
        filters     = filters_snap,
        jobs        = jobs,
    )

    # ── Persist to disk ───────────────────────────────────────────────────────
    try:
        payload = {
            "run_id":      run_id,
            "executed_at": now_iso,
            "status":      response.status,
            "total_found": len(jobs),
            "source":      src_label,
            "filters": {
                "keyword":             f.keyword,
                "location":            f.location,
                "job_type":            f.job_type,
                "work_mode":           f.work_mode,
                "search_window_hours": f.search_window_hours,
                "max_jobs":            f.max_jobs,
            },
            "jobs": [j.model_dump() for j in jobs],
        }
        saved_path       = _storage_svc.save_results(payload)
        response.saved_to = saved_path
    except Exception as exc:
        log.warning("harvest_save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-results", response_model=ResultsListResponse, status_code=status.HTTP_200_OK)
async def list_harvest_results() -> ResultsListResponse:
    """List all saved harvest run files, newest first."""
    results = _storage_svc.list_results()
    return ResultsListResponse(total_runs=len(results), results=results)


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_harvest_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No result found for run_id '{run_id}'",
        )
    return data


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-config
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-config", response_model=HarvestConfig, status_code=status.HTTP_200_OK)
async def get_harvest_config() -> HarvestConfig:
    """Return the current harvest configuration."""
    return _config_svc.load()


# ══════════════════════════════════════════════════════════════════════════════
# PUT /harvest-config
# ══════════════════════════════════════════════════════════════════════════════

@router.put("/harvest-config", response_model=HarvestConfig, status_code=status.HTTP_200_OK)
async def update_harvest_config(config: HarvestConfig, request: Request) -> HarvestConfig:
    """
    Save updated configuration to harvest_config.json.
    If schedule.enabled changes, the scheduler is updated immediately.
    """
    _config_svc.save(config)

    # Re-apply scheduler if available
    scheduler = _get_scheduler(request)
    if scheduler:
        await _apply_schedule(scheduler, config)

    return config


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-schedule/status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-schedule/status", response_model=ScheduleStatusResponse, status_code=status.HTTP_200_OK)
async def get_schedule_status(request: Request) -> ScheduleStatusResponse:
    """Return the current scheduler state."""
    config    = _config_svc.load()
    scheduler = _get_scheduler(request)
    next_run  = scheduler.get_next_run() if scheduler else None

    return ScheduleStatusResponse(
        enabled   = config.schedule.enabled,
        frequency = config.schedule.frequency,
        run_time  = config.schedule.run_time,
        timezone  = config.schedule.timezone,
        next_run  = next_run,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /harvest-schedule/toggle
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/harvest-schedule/toggle", status_code=status.HTTP_200_OK)
async def toggle_schedule(request: Request) -> Any:
    """Enable or disable the automatic harvest schedule (toggles current state)."""
    config          = _config_svc.load()
    config.schedule.enabled = not config.schedule.enabled
    _config_svc.save(config)

    scheduler = _get_scheduler(request)
    if scheduler:
        await _apply_schedule(scheduler, config)

    return {
        "status":  "enabled" if config.schedule.enabled else "disabled",
        "message": f"Scheduler {'enabled' if config.schedule.enabled else 'disabled'}",
        "next_run": scheduler.get_next_run() if scheduler else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Internal: apply schedule to SchedulerService
# ══════════════════════════════════════════════════════════════════════════════

async def _apply_schedule(scheduler, config: HarvestConfig) -> None:
    """Wire up the APScheduler job for the automatic harvest run."""
    from app.agents.orchestrator_agent import OrchestratorAgent  # avoid circular at import time
    from app.services.harvest_storage_service import HarvestStorageService

    storage = HarvestStorageService()

    async def _auto_harvest() -> None:
        cfg     = _config_svc.load()
        run_id  = _make_run_id(cfg.filters.keyword, cfg.filters.location)
        now_iso = datetime.now(timezone.utc).isoformat()
        orch    = OrchestratorAgent(cfg)
        if needs_proactor():
            jobs, src_label = await run_in_proactor(orch.run)
        else:
            jobs, src_label = await orch.run()
        payload = {
            "run_id":      run_id,
            "executed_at": now_iso,
            "status":      "success" if jobs else "no_results",
            "total_found": len(jobs),
            "source":      src_label,
            "filters":     cfg.filters.model_dump(),
            "jobs":        [j.model_dump() for j in jobs],
        }
        storage.save_results(payload)
        logger.info("scheduled_harvest_complete", run_id=run_id, total=len(jobs))

    scheduler.schedule_harvest(
        job_fn    = _auto_harvest,
        frequency = config.schedule.frequency,
        run_time  = config.schedule.run_time,
        timezone  = config.schedule.timezone,
        enabled   = config.schedule.enabled,
    )
