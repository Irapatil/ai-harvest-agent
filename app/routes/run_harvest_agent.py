"""
POST /run-harvest-agent  — fire-and-forget harvest trigger (frontend integration).
GET  /harvest-status/{job_id} — polling endpoint for live progress.
GET  /run-history             — list all completed runs.
GET  /run-history/{run_id}    — single run entry.

Design contract (Phase 3 — remove hardcoding)
─────────────────────────────────────────────
Old flow:  JSON Config → Harvest Agent
New flow:  Frontend → FastAPI (this file) → HarvestConfig built from payload → Harvest Agent

POST /run-harvest-agent
  • Accepts keyword / location / job_type / work_mode / search_window_hours / sources.
  • Merges with saved harvest_config.json for browser/schedule settings only.
  • Starts harvest as a background asyncio task.
  • Returns immediately:  { "job_id": "...", "status": "running", "message": "Harvest started" }

GET /harvest-status/{job_id}
  • Returns live progress: { status, progress, linkedin, naukri, dice, combined }
  • Frontend polls this every 30–60 s until status != "running".
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agents.orchestrator_agent import OrchestratorAgent, OrchestratorResult
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import (
    BrowserConfig,
    FiltersConfig,
    HarvestConfig,
    ScheduleConfig,
    SourcesConfig,
)
from app.models.unified_job import UnifiedJob
from app.services.config_service import ConfigService
from app.services.job_tracker import JobTracker
from app.services.run_history_service import RunHistoryService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Harvest Agent"])

_config_svc  = ConfigService()
_history_svc = RunHistoryService()
_RESULTS_BASE = Path("data/results")

_VALID_WINDOWS: set[int] = {24, 48, 72, 168, 720}
_VALID_SOURCES: set[str] = {"linkedin", "naukri", "dice"}


# ══════════════════════════════════════════════════════════════════════════════
# Request / Response models
# ══════════════════════════════════════════════════════════════════════════════

class HarvestRequest(BaseModel):
    """
    Frontend harvest trigger payload.

    All search parameters come from the UI.  Browser and schedule settings
    are still read from harvest_config.json so they are not re-sent on every call.
    """
    keyword:             str       = Field(
        "AI Engineer",
        description="Job search keyword (e.g. 'AI Engineer', 'Python Developer')",
        examples=["AI Engineer"],
    )
    location:            str       = Field(
        "India",
        description="Geographic filter (e.g. 'India', 'Bangalore', 'Remote')",
        examples=["India"],
    )
    job_type: Literal[
        "Contract", "Permanent", "Part-time", "Freelance", "Full-time", "Any"
    ] = Field(
        "Any",
        description="Employment type filter",
        examples=["Any"],
    )
    work_mode: Literal["Remote", "Hybrid", "Onsite", "Any"] = Field(
        "Any",
        description="Work arrangement filter",
        examples=["Any"],
    )
    search_window_hours: int = Field(
        24,
        description="Only return jobs posted within this many hours (24 | 48 | 72 | 168 | 720)",
        examples=[24],
    )
    sources: list[str] = Field(
        ["linkedin", "naukri", "dice"],
        description="Job boards to harvest from. Any subset of: linkedin, naukri, dice",
        examples=[["linkedin", "naukri", "dice"]],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "keyword":             "AI Engineer",
                "location":            "India",
                "job_type":            "Any",
                "work_mode":           "Any",
                "search_window_hours": 24,
                "sources":             ["linkedin", "naukri", "dice"],
            }
        }
    }


class HarvestStartResponse(BaseModel):
    job_id:  str
    status:  str
    message: str


class HarvestStatusResponse(BaseModel):
    status:   str
    progress: int
    linkedin: int
    naukri:   int
    dice:     int
    combined: int = 0
    run_id:   str = ""
    message:  str = ""
    error:    str | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _filters_snapshot(cfg: HarvestConfig) -> dict:
    f = cfg.filters
    return {
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
        "verification_enabled":       f.verification.enabled,
    }


def _save_source_results(
    run_id:      str,
    executed_at: str,
    source:      str,
    jobs:        list[UnifiedJob],
    filters_snap: dict,
) -> str:
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
    return str(path.resolve())


def _build_config(body: HarvestRequest) -> HarvestConfig:
    """
    Build a HarvestConfig from the UI payload.
    Browser and schedule settings are merged from the saved config file.
    """
    sources_list = [s.lower() for s in body.sources if s.lower() in _VALID_SOURCES]
    window = body.search_window_hours if body.search_window_hours in _VALID_WINDOWS else 24

    config = HarvestConfig(
        sources = SourcesConfig(
            linkedin = "linkedin" in sources_list,
            naukri   = "naukri"   in sources_list,
            dice     = "dice"     in sources_list,
        ),
        filters = FiltersConfig(
            keyword             = body.keyword,
            location            = body.location,
            job_type            = body.job_type,
            work_mode           = body.work_mode,
            search_window_hours = window,  # type: ignore[arg-type]
        ),
    )

    # Merge browser/schedule from saved config — don't expose them as UI params
    try:
        saved = _config_svc.load()
        config.browser  = saved.browser
        config.schedule = saved.schedule
    except Exception:
        pass

    return config


# ══════════════════════════════════════════════════════════════════════════════
# Background harvest coroutine
# ══════════════════════════════════════════════════════════════════════════════

async def _run_harvest_background(
    job_id:  str,
    run_id:  str,
    config:  HarvestConfig,
    now_iso: str,
) -> None:
    """
    Long-running harvest coroutine executed as a background asyncio task.

    Progress milestones written to JobTracker:
      10% — agents launched
     100% — complete (success or no_results)
       0% — failed
    """
    log = logger.bind(job_id=job_id, run_id=run_id)
    log.info("background_harvest_started")

    JobTracker.update(job_id, progress=10, message="Harvest agents running…")

    enabled = [
        s for s in ["naukri", "linkedin", "dice"]
        if getattr(config.sources, s, False)
    ]

    orch = OrchestratorAgent(config)

    try:
        if needs_proactor():
            result: OrchestratorResult = await run_in_proactor(orch.run_all)
        else:
            result = await orch.run_all()
    except Exception as exc:
        log.exception("background_harvest_error", error=str(exc))
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
            progress     = 0,
            message      = "Harvest failed",
            error        = str(exc),
            completed_at = datetime.now(timezone.utc).isoformat(),
        )
        return

    # ── Save per-source result files ──────────────────────────────────────────
    filters_snap = _filters_snapshot(config)
    for source, jobs in result.jobs_by_source.items():
        try:
            _save_source_results(run_id, now_iso, source, jobs, filters_snap)
        except Exception as exc:
            log.warning("source_save_failed", source=source, error=str(exc))

    # ── Update run history ────────────────────────────────────────────────────
    status_str = "success" if result.total_jobs > 0 else "no_results"
    _history_svc.append(
        RunHistoryService.make_entry(
            run_id         = run_id,
            sources        = result.sources_executed,
            started_at     = result.started_at,
            completed_at   = result.completed_at,
            status         = status_str,
            jobs_found     = result.total_jobs,
            verified_jobs  = result.verified_jobs,
            direct_clients = result.direct_clients,
            gcc            = result.gcc,
            staffing_firms = result.staffing_firms,
            ambiguous      = result.ambiguous,
        )
    )

    # ── Update job tracker ────────────────────────────────────────────────────
    JobTracker.update(
        job_id,
        status       = status_str,
        progress     = 100,
        message      = "Harvest complete" if status_str == "success" else "No results found",
        linkedin     = len(result.jobs_by_source.get("LinkedIn", [])),
        naukri       = len(result.jobs_by_source.get("Naukri",   [])),
        dice         = len(result.jobs_by_source.get("Dice",     [])),
        combined     = result.total_jobs,
        run_id       = run_id,
        completed_at = result.completed_at.isoformat(),
        excel_path   = result.excel_path   or "",
        json_path    = result.combined_path or "",
    )

    log.info(
        "background_harvest_complete",
        total      = result.total_jobs,
        status     = status_str,
        excel_path = result.excel_path,
        json_path  = result.combined_path,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/run-harvest-agent",
    status_code       = status.HTTP_202_ACCEPTED,
    response_model    = HarvestStartResponse,
    summary           = "Start harvest",
    description       = (
        "Triggers a full harvest run in the background and returns immediately. "
        "Poll **GET /harvest-status/{job_id}** every 30–60 s for progress. "
        "Naukri typically takes 40–90 minutes; LinkedIn and Dice finish in 3–15 minutes."
    ),
    responses         = {
        202: {"description": "Harvest accepted — running in background"},
        400: {"description": "No valid sources provided"},
        422: {"description": "Validation error"},
    },
)
async def run_harvest_agent(body: HarvestRequest = HarvestRequest()) -> HarvestStartResponse:
    """
    Start a harvest.  Returns a `job_id` immediately — the harvest runs
    in the background.  Use **GET /harvest-status/{job_id}** to track progress.
    """
    sources_list = [s.lower() for s in body.sources if s.lower() in _VALID_SOURCES]
    if not sources_list:
        raise HTTPException(
            status_code = 400,
            detail      = "At least one valid source required: linkedin, naukri, dice",
        )

    job_id  = str(uuid4())
    run_id  = _make_run_id()
    now_iso = datetime.now(timezone.utc).isoformat()
    config  = _build_config(body)

    log = logger.bind(job_id=job_id, run_id=run_id, sources=sources_list)
    log.info(
        "harvest_agent_start",
        keyword              = body.keyword,
        location             = body.location,
        job_type             = body.job_type,
        work_mode            = body.work_mode,
        search_window_hours  = body.search_window_hours,
    )

    JobTracker.create(job_id, run_id)

    # Fire background task — does not block the HTTP response
    asyncio.create_task(
        _run_harvest_background(job_id, run_id, config, now_iso),
        name=f"harvest-{job_id}",
    )

    return HarvestStartResponse(
        job_id  = job_id,
        status  = "running",
        message = "Harvest started",
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_LABEL: dict[str, str] = {
    "running":    "Running",
    "success":    "Success",
    "no_results": "No Results",
    "failed":     "Failed",
}


@router.get(
    "/harvest-status/{job_id}",
    status_code    = status.HTTP_200_OK,
    response_model = HarvestStatusResponse,
    summary        = "Harvest job progress",
    description    = (
        "Poll this endpoint every 30–60 s after starting a harvest. "
        "When `status` is no longer `Running`, the harvest is done."
    ),
    responses = {
        200: {"description": "Job status"},
        404: {"description": "job_id not found"},
    },
)
async def get_harvest_status(job_id: str) -> HarvestStatusResponse:
    """
    Returns live progress for a running harvest.

    | status       | meaning                       |
    |--------------|-------------------------------|
    | Running      | agents still executing        |
    | Success      | harvest complete with results |
    | No Results   | harvest complete, 0 jobs      |
    | Failed       | an error occurred             |
    """
    js = JobTracker.get(job_id)
    if js is None:
        raise HTTPException(
            status_code = 404,
            detail      = f"No harvest job found with id '{job_id}'",
        )
    return HarvestStatusResponse(
        status   = _STATUS_LABEL.get(js.status, js.status.title()),
        progress = js.progress,
        linkedin = js.linkedin,
        naukri   = js.naukri,
        dice     = js.dice,
        combined = js.combined,
        run_id   = js.run_id,
        message  = js.message,
        error    = js.error or None,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /run-history
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/run-history",
    status_code = status.HTTP_200_OK,
    summary     = "List all harvest runs",
    description = "Returns all past harvest run history entries, newest first.",
)
async def get_run_history() -> Any:
    runs = _history_svc.list_all()
    return {"total_runs": len(runs), "runs": runs}


@router.get(
    "/run-history/{run_id}",
    status_code = status.HTTP_200_OK,
    summary     = "Single run history entry",
    description = "Returns one run history entry by run_id.",
    responses   = {404: {"description": "run_id not found"}},
)
async def get_run_history_entry(run_id: str) -> Any:
    entry = _history_svc.get(run_id)
    if entry is None:
        raise HTTPException(
            status_code = 404,
            detail      = f"No run history entry found for run_id '{run_id}'",
        )
    return entry
