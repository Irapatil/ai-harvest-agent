"""
Lead Intelligence API routes.

POST /run-lead-intelligence      — start hybrid LinkedIn + Naukri lead harvest (async)
GET  /lead-intelligence          — paginated list of all saved lead records
GET  /lead-intelligence/{id}     — single lead record by lead_id
GET  /download/lead-intelligence/json   — download latest leads JSON file
GET  /download/lead-intelligence/excel  — download latest leads Excel file
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.models.lead_models import LeadIntelligenceRequest, LeadIntelligenceResult

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Lead Intelligence"])

_LEAD_OUTPUT_DIR = Path("data/results/lead_intelligence")

# ── In-memory job tracker for lead intelligence runs ──────────────────────────
# (lightweight — reuses same pattern as JobTracker for harvest runs)
_lead_jobs: dict[str, dict[str, Any]] = {}


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-lead-intelligence
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/run-lead-intelligence",
    status_code = 202,
    summary     = "Start Lead Intelligence Run",
    description = (
        "Launches a background Hybrid Lead Intelligence run.\n\n"
        "Flow: LinkedIn post search → Premium Naukri fallback → "
        "Merge + Dedup → Confidence validation → JSON + Excel output.\n\n"
        "Returns a `job_id` immediately. Poll `GET /lead-intelligence-status/{job_id}` "
        "to track progress."
    ),
    responses = {
        202: {"description": "Lead intelligence run accepted and started"},
        422: {"description": "Validation error in request body"},
    },
)
async def run_lead_intelligence(body: LeadIntelligenceRequest) -> dict[str, Any]:
    import uuid
    job_id = uuid.uuid4().hex

    _lead_jobs[job_id] = {
        "job_id":    job_id,
        "status":    "running",
        "progress":  0,
        "message":   "Lead intelligence run started",
        "keyword":   body.keyword,
        "max_leads": body.max_leads,
        "total":     0,
        "json_path": "",
        "excel_path": "",
        "error":     "",
    }

    logger.info(
        "lead_intelligence_run_accepted",
        job_id  = job_id,
        keyword = body.keyword,
    )

    asyncio.create_task(
        _run_lead_intelligence_background(job_id, body),
        name=f"lead_intelligence_{job_id}",
    )

    return {
        "job_id":   job_id,
        "status":   "running",
        "message":  "Lead intelligence run started. Poll /lead-intelligence-status/{job_id}.",
        "keyword":  body.keyword,
    }


async def _run_lead_intelligence_background(
    job_id: str,
    request: LeadIntelligenceRequest,
) -> None:
    """Background coroutine — runs the full lead pipeline and updates _lead_jobs."""
    from app.agents.lead_intelligence_orchestrator import LeadIntelligenceOrchestrator
    from app.core.proactor import run_in_proactor

    _lead_jobs[job_id]["progress"] = 10
    _lead_jobs[job_id]["message"]  = "Running LinkedIn + Naukri pipeline…"

    try:
        orch = LeadIntelligenceOrchestrator()
        result: LeadIntelligenceResult = await run_in_proactor(
            lambda: orch.run(request)
        )
        _lead_jobs[job_id].update({
            "status":     "success",
            "progress":   100,
            "message":    f"Completed — {result.total_leads} leads found",
            "total":      result.total_leads,
            "high":       result.high_confidence,
            "medium":     result.medium_confidence,
            "low":        result.low_confidence,
            "json_path":  result.json_path,
            "excel_path": result.excel_path,
        })
        logger.info("lead_intelligence_run_completed", job_id=job_id, total=result.total_leads)

    except Exception as exc:
        logger.exception("lead_intelligence_run_failed", job_id=job_id, error=str(exc))
        _lead_jobs[job_id].update({
            "status":   "failed",
            "progress": 0,
            "message":  "Lead intelligence run failed",
            "error":    str(exc),
        })


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence-status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence-status/{job_id}",
    summary = "Poll Lead Intelligence Run Status",
)
async def lead_intelligence_status(job_id: str) -> dict[str, Any]:
    job = _lead_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence  (paginated list)
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence",
    summary     = "List Lead Intelligence Records",
    description = "Return all saved lead records (paginated). Reads from the latest leads JSON file.",
    responses   = {
        200: {"description": "Paginated lead records"},
        404: {"description": "No lead intelligence results found yet"},
    },
)
async def list_lead_intelligence(
    page:             int   = Query(1,  ge=1,  description="Page number"),
    page_size:        int   = Query(50, ge=1,  le=500, description="Records per page"),
    confidence_score: str   = Query("", description="Filter: High | Medium | Low"),
    source:           str   = Query("", description="Filter by source: linkedin | premium_naukri"),
    keyword:          str   = Query("", description="Filter by keyword (case-insensitive)"),
) -> dict[str, Any]:
    records = _load_lead_records()
    if records is None:
        raise HTTPException(status_code=404, detail="No lead intelligence results found yet.")

    # ── Filter ────────────────────────────────────────────────────────────────
    if confidence_score:
        records = [r for r in records if r.get("confidence_score", "") == confidence_score]
    if source:
        records = [r for r in records if source.lower() in [s.lower() for s in r.get("source", [])]]
    if keyword:
        kw = keyword.lower()
        records = [
            r for r in records
            if kw in r.get("recruiter_name", "").lower()
            or kw in r.get("company", "").lower()
            or kw in r.get("designation", "").lower()
        ]

    total       = len(records)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start       = (page - 1) * page_size
    page_data   = records[start : start + page_size]

    return {
        "page":        page,
        "page_size":   page_size,
        "total":       total,
        "total_pages": total_pages,
        "records":     page_data,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence/{lead_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence/{lead_id}",
    summary     = "Get Single Lead Record",
    description = "Return one lead record by its `lead_id`.",
    responses   = {
        200: {"description": "Lead record found"},
        404: {"description": "Lead not found"},
    },
)
async def get_lead_record(lead_id: str) -> dict[str, Any]:
    records = _load_lead_records()
    if records is None:
        raise HTTPException(status_code=404, detail="No lead intelligence results found yet.")

    for record in records:
        if record.get("lead_id") == lead_id or _lead_id(record) == lead_id:
            return record

    raise HTTPException(status_code=404, detail=f"Lead {lead_id!r} not found.")


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/lead-intelligence/json
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/lead-intelligence/json",
    summary     = "Download Lead Intelligence JSON",
    description = "Download the most recent lead intelligence run output as JSON.",
    responses   = {
        200: {"description": "JSON file download"},
        404: {"description": "No lead intelligence output file found"},
    },
)
async def download_lead_intelligence_json() -> FileResponse:
    path = _latest_file("*.json")
    if not path:
        raise HTTPException(status_code=404, detail="No lead intelligence JSON file found.")

    return FileResponse(
        path              = str(path),
        media_type        = "application/json",
        filename          = path.name,
        headers           = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/lead-intelligence/excel
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/lead-intelligence/excel",
    summary     = "Download Lead Intelligence Excel",
    description = "Download the most recent lead intelligence run output as Excel (.xlsx).",
    responses   = {
        200: {"description": "Excel file download"},
        404: {"description": "No lead intelligence Excel file found"},
    },
)
async def download_lead_intelligence_excel() -> FileResponse:
    path = _latest_file("*.xlsx")
    if not path:
        raise HTTPException(status_code=404, detail="No lead intelligence Excel file found.")

    return FileResponse(
        path       = str(path),
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename   = path.name,
        headers    = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _latest_file(glob_pattern: str) -> Path | None:
    """Return the most recently modified file matching glob_pattern in the lead output dir."""
    files = sorted(
        _LEAD_OUTPUT_DIR.glob(glob_pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if _LEAD_OUTPUT_DIR.exists() else []
    return files[0] if files else None


def _load_lead_records() -> list[dict[str, Any]] | None:
    """Load lead records from the latest JSON output file."""
    path = _latest_file("*.json")
    if not path:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data.get("leads", data) if isinstance(data, dict) else data
        if not isinstance(records, list):
            return None
        # Ensure each record has an `id` field for stable lookup
        for r in records:
            if "lead_id" not in r:
                r["lead_id"] = _lead_id(r)
        return records
    except Exception as exc:
        logger.warning("lead_records_load_failed", error=str(exc))
        return None


def _lead_id(record: dict[str, Any]) -> str:
    """Compute a stable 16-char ID from linkedin_profile_url or name::company."""
    url = record.get("linkedin_profile_url", "").strip()
    if url:
        key = url
    else:
        name    = record.get("recruiter_name", "").strip().lower()
        company = record.get("company", "").strip().lower()
        key     = f"{name}::{company}"
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:16]
