"""
Recruiter Contact Discovery API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST  /run-recruiter-discovery        enrich recruiters from harvest results
  GET   /recruiter-results              list all past recruiter discovery runs
  GET   /recruiter-results/{run_id}     retrieve one run's full JSON output

Design contract
───────────────
• Reads recruiters from existing harvest result JSON files (no re-scraping jobs)
• All enrichment logic lives in RecruiterContactAgent — this route is a thin trigger
• Output format matches the Lead Intelligence Report (same 13 columns + Summary)
• DO NOT modify authentication, Chrome profile persistence, or session handling
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agents.recruiter_contact_agent import RecruiterContactAgent
from app.core.proactor import needs_proactor, run_in_proactor

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["Recruiter Contact Discovery"])

_OUTPUT_DIR = Path("data/results/lead_intelligence")


# ── Request model ──────────────────────────────────────────────────────────────

class RecruiterDiscoveryRequest(BaseModel):
    """
    Trigger payload for recruiter contact discovery.

    All fields are optional — sensible defaults apply.
    """
    source_filter: str = Field(
        default="all",
        description=(
            "Which harvest sources to pull recruiters from: "
            "'all' (default) | 'combined' | 'linkedin' | 'naukri' | 'dice'"
        ),
    )
    run_ids: list[str] = Field(
        default=[],
        description=(
            "Optional list of specific harvest run ID prefixes to process "
            "(e.g. ['20260622_045415']). Leave empty to use all available runs."
        ),
    )
    max_files: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max number of result files to load per source (newest first, default 10).",
    )
    concurrency: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Max parallel browser sessions for contact enrichment (1–5, default 2).",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /run-recruiter-discovery
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/run-recruiter-discovery", status_code=status.HTTP_200_OK)
async def run_recruiter_discovery(
    body: RecruiterDiscoveryRequest = RecruiterDiscoveryRequest(),
) -> Any:
    """
    **Recruiter Contact Discovery Agent**

    Reads job-poster / recruiter records from completed harvest runs
    (LinkedIn, Naukri, Dice), deduplicates them, then enriches each
    recruiter using a 3-level scraped contact discovery pipeline:

    | Level | Method                          | Email Status |
    |-------|---------------------------------|--------------|
    | 1     | Company website scraping        | VERIFIED     |
    | 2     | DuckDuckGo → LinkedIn profile   | PUBLIC       |
    | 3     | Naukri cross-source search      | PUBLIC       |
    | —     | Not publicly available          | NOT_FOUND    |

    **Output** (saved to `data/results/lead_intelligence/`):
    - `<run_id>_Recruiter_Contact_Report.xlsx`  — consolidated Excel (13 cols + Summary)
    - `<run_id>_recruiter_contacts.json`         — full JSON with enrichment audit logs
    - `<run_id>_diagnostics.json`                — per-recruiter diagnostics

    **Confidence rules:**
    - `High`   — LinkedIn URL + VERIFIED/PUBLIC email + company match
    - `Medium` — LinkedIn URL + company match (no email found)
    - `Low`    — No LinkedIn URL resolved

    **Important:** Contact details are NEVER fabricated, predicted, or guessed.
    Only emails and phones actually scraped from public sources are stored.
    """
    agent = RecruiterContactAgent(concurrency=body.concurrency)

    async def _run():
        return await agent.run(
            source_filter = body.source_filter,
            run_ids       = body.run_ids or None,
            max_files     = body.max_files,
        )

    try:
        if needs_proactor():
            result = await run_in_proactor(_run)
        else:
            result = await _run()
    except Exception as exc:
        logger.exception("recruiter_discovery_error", error=str(exc))
        return JSONResponse(
            status_code=200,
            content={
                "status":  "failed",
                "message": "Recruiter contact discovery failed",
                "reason":  str(exc),
            },
        )

    if result.total_recruiters == 0:
        return JSONResponse(
            status_code=200,
            content={
                "status":  "no_data",
                "message": (
                    f"No recruiters found in harvest results "
                    f"(source_filter='{body.source_filter}', "
                    f"run_ids={body.run_ids or 'all'})."
                ),
                "hint": (
                    "Run a harvest first (POST /run-harvest-agent) to populate recruiter data. "
                    "Only jobs with a named job poster contribute to this report."
                ),
            },
        )

    return {
        "run_id":          result.run_id,
        "status":          "success",
        "harvest_sources": result.harvest_sources,
        "total_recruiters": result.total_recruiters,
        "enriched":         result.enriched,
        "high_confidence":  result.high_confidence,
        "medium_confidence":result.medium_confidence,
        "low_confidence":   result.low_confidence,
        "contact_discovery": {
            "verified_emails": result.verified_emails,
            "public_emails":   result.public_emails,
            "verified_phones": result.verified_phones,
            "public_phones":   result.public_phones,
            "no_contact":      result.no_contact,
        },
        "runtime_minutes": result.runtime_minutes,
        "excel_path":      result.excel_path,
        "json_path":       result.json_path,
        "output_dir":      str(_OUTPUT_DIR.resolve()),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GET /recruiter-results
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/recruiter-results", status_code=status.HTTP_200_OK)
async def list_recruiter_results() -> Any:
    """List all saved recruiter contact discovery JSON result files, newest first."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        _OUTPUT_DIR.glob("rcd_*_recruiter_contacts.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    entries = []
    for f in files:
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "run_id":           meta.get("run_id", f.stem),
                "source_filter":    meta.get("source_filter", ""),
                "total":            meta.get("total", 0),
                "enriched":         meta.get("enriched", 0),
                "verified_emails": meta.get("verified_emails", 0),
                "public_emails":   meta.get("public_emails", 0),
                "high":            meta.get("high", 0),
                "medium":           meta.get("medium", 0),
                "low":              meta.get("low", 0),
                "json_path":        str(f.resolve()),
            })
        except Exception:
            entries.append({"file": str(f), "error": "Could not parse"})
    return {"total_runs": len(entries), "runs": entries}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /recruiter-results/{run_id}
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/recruiter-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_recruiter_result(run_id: str) -> Any:
    """Retrieve the full JSON output of one recruiter contact discovery run."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _OUTPUT_DIR / f"{run_id}_recruiter_contacts.json"
    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"No result found for run_id '{run_id}'"},
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Could not read result file: {exc}"},
        )
