"""
POST /run-harvest-agent — unified harvest trigger (async / background).
GET  /harvest-status/{job_id} — live status polling.

Design contract
───────────────
• POST accepts ONLY {} or {"config_id": "active"}.  No search criteria.
  All harvesting parameters come from harvest_config.json.
• The endpoint returns HTTP 202 immediately with a job_id.
  The harvest runs in the background and can be polled via GET /harvest-status/{job_id}.

Execution flow (background task)
────────────────────────────────
1. Load harvest_config.json  (config_service)
2. Determine enabled sources
3. Run sources in priority order: Naukri → LinkedIn → Dice  (via OrchestratorAgent)
4. Apply business filters (domain / hiring entity / GCC / salary / work mode)
5. Run company verification if enabled
6. Save per-source result files to data/results/<source>/
7. Update data/results/run_history/run_history.json
8. Mark job complete in JobTracker

Response (202)
──────────────
{
    "job_id":  "a1b2c3d4...",
    "status":  "running",
    "message": "Harvest started in background"
}

Poll GET /harvest-status/{job_id} for progress and final results.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.orchestrator_agent import OrchestratorAgent, OrchestratorResult
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.unified_job import UnifiedJob
from app.services.config_service import ConfigService
from app.services.job_tracker import JobTracker
from app.services.run_history_service import RunHistoryService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Harvest Agent"])

_config_svc  = ConfigService()
_history_svc = RunHistoryService()

_RESULTS_BASE = Path("data/results")


# ── Request model ──────────────────────────────────────────────────────────────

class HarvestAgentRequest(BaseModel):
    """
    Execution trigger payload.

    Only config_id is accepted — no search criteria.
    All harvesting parameters are loaded from harvest_config.json.
    """
    config_id: str = "active"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _filters_snapshot(cfg) -> dict:
    f = cfg.filters
    return {
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
        "verification_enabled":     f.verification.enabled,
    }


def _save_source_results(
    run_id:      str,
    executed_at: str,
    source:      str,
    jobs:        list[UnifiedJob],
    filters_snap: dict,
) -> str:
    """
    Save one source's jobs to data/results/<source_lower>/YYYYMMDD_HHMMSS_<source_lower>.json.
    Returns the absolute path of the saved file.
    """
    source_lower = source.lower()
    out_dir      = _RESULTS_BASE / source_lower
    out_dir.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{source_lower}.json"
    payload  = {
        "run_id":      run_id,
        "executed_at": executed_at,
        "source":      source,
        "total_found": len(jobs),
        "filters":     filters_snap,
        "jobs":        [j.to_dict() for j in jobs],
    }
    path = out_dir / filename
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("source_results_saved", source=source, path=str(path), count=len(jobs))
    if source.lower() == "linkedin":
        logger.info("linkedin_json_saved", path=str(path), count=len(jobs))
    return str(path.resolve())


# ══════════════════════════════════════════════════════════════════════════════
# Background harvest task
# ══════════════════════════════════════════════════════════════════════════════

async def _run_harvest_background(
    job_id:   str,
    run_id:   str,
    config:   Any,
    now_iso:  str,
    enabled:  list[str],
) -> None:
    """Runs the full harvest in a background asyncio task, updating JobTracker."""
    log = logger.bind(job_id=job_id, run_id=run_id, sources=enabled)
    log.info("harvest_background_start")

    JobTracker.update(job_id, progress=10, message="Starting orchestrator")

    orch = OrchestratorAgent(config)

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            result: OrchestratorResult = await run_in_proactor(orch.run_all)
        else:
            result = await orch.run_all()
    except Exception as exc:
        log.exception("harvest_background_error", error=str(exc))
        _history_svc.append(
            RunHistoryService.make_entry(
                run_id       = run_id,
                sources      = enabled,
                started_at   = datetime.now(timezone.utc),
                completed_at = datetime.now(timezone.utc),
                status       = "failed",
                jobs_found   = 0,
            )
        )
        JobTracker.update(
            job_id,
            status       = "failed",
            progress     = 100,
            message      = f"Harvest failed: {exc}",
            error        = str(exc),
            completed_at = datetime.now(timezone.utc).isoformat(),
        )
        return

    JobTracker.update(
        job_id,
        progress = 70,
        message  = "Saving results",
        linkedin = len(result.jobs_by_source.get("LinkedIn", [])),
        naukri   = len(result.jobs_by_source.get("Naukri",   [])),
        dice     = len(result.jobs_by_source.get("Dice",     [])),
        combined = result.total_jobs,
    )

    # ── Save per-source result files ──────────────────────────────────────────
    filters_snap = _filters_snapshot(config)

    for source, jobs in result.jobs_by_source.items():
        try:
            _save_source_results(run_id, now_iso, source, jobs, filters_snap)
        except Exception as exc:
            log.warning("source_save_failed", source=source, error=str(exc))

    # ── Update run history ────────────────────────────────────────────────────
    status_str = "success" if result.total_jobs > 0 else "no_results"
    history_entry = RunHistoryService.make_entry(
        run_id          = run_id,
        sources         = result.sources_executed,
        started_at      = result.started_at,
        completed_at    = result.completed_at,
        status          = status_str,
        jobs_found      = result.total_jobs,
        verified_jobs   = result.verified_jobs,
        direct_clients  = result.direct_clients,
        gcc             = result.gcc,
        staffing_firms  = result.staffing_firms,
        ambiguous       = result.ambiguous,
    )
    try:
        _history_svc.append(history_entry)
    except Exception as exc:
        log.warning("history_save_failed", error=str(exc))

    elapsed_seconds = (result.completed_at - result.started_at).total_seconds()
    log.info(
        "harvest_completed",
        run_id         = run_id,
        total          = result.total_jobs,
        verified       = result.verified_jobs,
        direct_clients = result.direct_clients,
        gcc            = result.gcc,
        staffing_firms = result.staffing_firms,
        ambiguous      = result.ambiguous,
        sources        = result.sources_executed,
        runtime_min    = round(elapsed_seconds / 60, 1),
        combined_path  = result.combined_path,
    )

    JobTracker.update(
        job_id,
        status       = status_str,
        progress     = 100,
        message      = f"Harvest complete — {result.total_jobs} jobs found",
        completed_at = result.completed_at.isoformat(),
        excel_path   = result.excel_path   or "",
        json_path    = result.combined_path or "",
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-harvest-agent", status_code=status.HTTP_202_ACCEPTED)
async def run_harvest_agent(body: HarvestAgentRequest = HarvestAgentRequest()) -> Any:
    """
    Trigger a full harvest run from the current harvest_config.json settings.

    Returns immediately (HTTP 202) with a `job_id`.
    Poll **GET /harvest-status/{job_id}** for live progress and final results.

    All search filters (keyword, location, job_type, work_mode, domain,
    hiring_entity, GCC mode, salary, verification) are read from the saved
    config — this endpoint accepts **no** filter parameters.

    Execution order: Naukri (1) → LinkedIn (2) → Dice (3)
    """
    config  = _config_svc.load()
    run_id  = _make_run_id()
    now_iso = datetime.now(timezone.utc).isoformat()

    logger.info(
        "config_loaded",
        keyword              = config.filters.keyword,
        location             = config.filters.location,
        job_type             = config.filters.job_type,
        work_mode            = config.filters.work_mode,
        search_window_hours  = config.filters.search_window_hours,
        max_jobs             = config.filters.max_jobs,
        domain               = config.filters.domain,
        hiring_entity        = config.filters.hiring_entity,
    )

    enabled = [
        src for src in ["naukri", "linkedin", "dice"]
        if getattr(config.sources, src, False)
    ]
    if not enabled:
        return _err(
            "No sources enabled",
            "Enable at least one source (linkedin, naukri, or dice) in harvest_config.json",
        )

    job_id = uuid4().hex
    JobTracker.create(job_id, run_id)

    logger.info(
        "harvest_agent_queued",
        job_id    = job_id,
        run_id    = run_id,
        sources   = enabled,
        keyword   = config.filters.keyword,
        config_id = body.config_id,
    )

    asyncio.create_task(
        _run_harvest_background(job_id, run_id, config, now_iso, enabled),
        name = f"harvest-{job_id}",
    )

    return JSONResponse(
        status_code = status.HTTP_202_ACCEPTED,
        content = {
            "job_id":  job_id,
            "run_id":  run_id,
            "status":  "running",
            "message": "Harvest started in background — poll GET /harvest-status/{job_id}",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-status/{job_id}", status_code=status.HTTP_200_OK)
async def get_harvest_status(job_id: str) -> Any:
    """
    Poll the status of a background harvest job.

    Returns progress (0–100), per-source counts, and output file paths
    once the harvest completes.

    Possible `status` values:
    - `running`    — harvest is in progress
    - `success`    — harvest completed with results
    - `no_results` — harvest completed but found no matching jobs
    - `failed`     — harvest error (check `error` field)
    """
    js = JobTracker.get(job_id)
    if js is None:
        return JSONResponse(
            status_code = 404,
            content     = {"detail": f"No harvest job found with id '{job_id}'"},
        )
    return js.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# GET /run-history
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/run-history", status_code=status.HTTP_200_OK)
async def get_run_history() -> Any:
    """Return all harvest run history entries, newest first."""
    return {"total_runs": len(_history_svc.list_all()), "runs": _history_svc.list_all()}


@router.get("/run-history/{run_id}", status_code=status.HTTP_200_OK)
async def get_run_history_entry(run_id: str) -> Any:
    """Return a single run history entry by run_id."""
    entry = _history_svc.get(run_id)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No run history entry found for run_id '{run_id}'"},
        )
    return entry
