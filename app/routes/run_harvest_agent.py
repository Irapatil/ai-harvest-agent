"""
POST /run-harvest-agent — unified harvest trigger.

Design contract
───────────────
• Request body accepts ONLY {} or {"config_id": "active"}.
• No search criteria, keywords, locations, job types, or salary values
  are accepted in the request.  All harvesting parameters come exclusively
  from the saved harvest_config.json written by the UI Rule Engine.
• The endpoint is a pure execution trigger: load config → run → report.

Execution flow
──────────────
1. Load harvest_config.json  (config_service)
2. Determine enabled sources
3. Run sources in priority order: Naukri → LinkedIn → Dice
4. Apply business filters (domain / hiring entity / GCC / salary / work mode)
5. Run company verification if enabled
6. Save per-source result files to data/results/<source>/
7. Update data/results/run_history/run_history.json
8. Return execution summary

Response
────────
{
    "run_id":           "20260601_143000",
    "status":           "success",
    "sources_executed": ["Naukri", "LinkedIn"],
    "jobs_found":       25,
    "verified_jobs":    0,
    "saved_to":         "data/results"
}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.orchestrator_agent import OrchestratorAgent, OrchestratorResult
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.unified_job import UnifiedJob
from app.services.config_service import ConfigService
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
    return str(path.resolve())


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-harvest-agent", status_code=status.HTTP_200_OK)
async def run_harvest_agent(body: HarvestAgentRequest = HarvestAgentRequest()) -> Any:
    """
    Trigger a full harvest run from the current harvest_config.json settings.

    All search filters (keyword, location, job_type, work_mode, domain,
    hiring_entity, GCC mode, salary, verification) are read from the saved
    config — this endpoint accepts **no** filter parameters.

    Execution order: Naukri (1) → LinkedIn (2) → Dice (3)
    """
    config  = _config_svc.load()
    run_id  = _make_run_id()
    now_iso = datetime.now(timezone.utc).isoformat()

    enabled = [
        src for src in ["naukri", "linkedin", "dice"]
        if getattr(config.sources, src, False)
    ]
    if not enabled:
        return _err(
            "No sources enabled",
            "Enable at least one source (linkedin, naukri, or dice) in harvest_config.json",
        )

    log = logger.bind(
        run_id    = run_id,
        config_id = body.config_id,
        sources   = enabled,
        keyword   = config.filters.keyword,
    )
    log.info("harvest_agent_start")

    # ── Run orchestrator ──────────────────────────────────────────────────────
    orch = OrchestratorAgent(config)

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            result: OrchestratorResult = await run_in_proactor(orch.run_all)
        else:
            result = await orch.run_all()
    except Exception as exc:
        log.exception("harvest_agent_error", error=str(exc))
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
        return _err("Harvest failed", str(exc) or "Unexpected scraping error")

    # ── Save per-source result files ──────────────────────────────────────────
    filters_snap = _filters_snapshot(config)
    saved_paths: list[str] = []

    for source, jobs in result.jobs_by_source.items():
        try:
            path = _save_source_results(run_id, now_iso, source, jobs, filters_snap)
            saved_paths.append(path)
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

    log.info(
        "harvest_agent_done",
        total          = result.total_jobs,
        verified       = result.verified_jobs,
        direct_clients = result.direct_clients,
        gcc            = result.gcc,
        staffing_firms = result.staffing_firms,
        ambiguous      = result.ambiguous,
        sources        = result.sources_executed,
    )

    source_counts = {
        f"{src.lower()}_jobs": len(jobs)
        for src, jobs in result.jobs_by_source.items()
    }

    return {
        "run_id":           run_id,
        "status":           status_str,
        "sources_executed": result.sources_executed,
        **source_counts,
        "total_jobs":       result.total_jobs,
        "jobs_found":       result.total_jobs,
        "direct_clients":   result.direct_clients,
        "gcc":              result.gcc,
        "staffing_firms":   result.staffing_firms,
        "ambiguous":        result.ambiguous,
        "verified_jobs":    result.verified_jobs,
        "saved_to":         "data/results",
        "filters_applied":  filters_snap,
    }


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
