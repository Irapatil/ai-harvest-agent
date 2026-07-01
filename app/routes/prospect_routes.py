"""
Prospect Intelligence Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST  /run-prospect-intelligence   enrich a prospects.xlsx file
  GET   /prospect-results            list all past lead intelligence runs
  GET   /prospect-results/{run_id}   retrieve one run's JSON output

Design contract
───────────────
• Input file path is configurable via request body (default: data/prospects/input/prospects.xlsx)
• All enrichment logic lives in ProspectIntelligenceAgent — this route is a thin trigger
• Response includes run summary + paths to Excel and JSON outputs
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agents.prospect_intelligence_agent import ProspectIntelligenceAgent
from app.core.proactor import needs_proactor, run_in_proactor

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["Prospect Intelligence Agent"])

_DEFAULT_INPUT   = "data/prospects/input/prospects.xlsx"
_OUTPUT_DIR      = Path("data/results/lead_intelligence")


# ── Request model ──────────────────────────────────────────────────────────────

class ProspectIntelligenceRequest(BaseModel):
    """
    Trigger payload for prospect intelligence enrichment.
    All fields are optional — sensible defaults apply.
    """
    input_file:  str = Field(
        default=_DEFAULT_INPUT,
        description="Path to prospects.xlsx (columns: Client Name, Poc Name, Designation)",
    )
    concurrency: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Max parallel LinkedIn searches (1–5; default 2 to avoid rate-limiting)",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /run-prospect-intelligence
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/run-prospect-intelligence", status_code=status.HTTP_200_OK)
async def run_prospect_intelligence(
    body: ProspectIntelligenceRequest = ProspectIntelligenceRequest(),
) -> Any:
    """
    **Prospect Intelligence Agent**

    Reads a prospects.xlsx file and enriches every row with:
    - LinkedIn profile URL, headline, location
    - Company website and domain
    - Predicted business email patterns *(marked as Predicted — never verified)*
    - Department inference
    - Reporting hierarchy (TA/HR leads at same company)
    - Confidence score (High / Medium / Low)

    **Input file format** (prospects.xlsx):

    | Client Name | Poc Name | Designation | LinkedIn |
    |---|---|---|---|
    | Kiya AI | Rajesh Mirjankar | MD & CEO | |
    | | Prem Kumar | Sr. HR | |

    Company name uses forward-fill (blank = same company as previous row).

    **Output**:
    - `data/results/lead_intelligence/<run_id>_Lead_Intelligence_Report.xlsx`
    - `data/results/lead_intelligence/<run_id>_lead_intelligence.json`
    - Intermediate JSON saves every 25 records
    """
    input_path = body.input_file
    if not Path(input_path).exists():
        return JSONResponse(
            status_code=200,
            content={
                "status":  "failed",
                "message": f"Input file not found: {input_path}",
                "hint":    f"Upload your prospects.xlsx to {_DEFAULT_INPUT} or specify a custom path.",
            },
        )

    logger.info(
        "prospect_intelligence_triggered",
        input_file  = input_path,
        concurrency = body.concurrency,
    )

    agent = ProspectIntelligenceAgent(concurrency=body.concurrency)

    async def _run():
        return await agent.run(input_path)

    try:
        if needs_proactor():
            result = await run_in_proactor(_run)
        else:
            result = await _run()
    except FileNotFoundError as exc:
        return JSONResponse(status_code=200, content={"status": "failed", "message": str(exc)})
    except Exception as exc:
        logger.exception("prospect_intelligence_error", error=str(exc))
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Prospect intelligence run failed", "reason": str(exc)},
        )

    return {
        "run_id":            result.run_id,
        "status":            "success",
        "total_prospects":   result.total_prospects,
        "enriched":          result.enriched,
        "high_confidence":   result.high_confidence,
        "medium_confidence": result.medium_confidence,
        "low_confidence":    result.low_confidence,
        "contact_discovery": {
            "verified_emails": result.verified_emails,
            "public_emails":   result.public_emails,
            "verified_phones": result.verified_phones,
            "public_phones":   result.public_phones,
            "no_contact":      result.no_contact,
        },
        "runtime_minutes":   result.runtime_minutes,
        "excel_path":        result.excel_path,
        "json_path":         result.json_path,
        "output_dir":        str(_OUTPUT_DIR.resolve()),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GET /prospect-results
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/prospect-results", status_code=status.HTTP_200_OK)
async def list_prospect_results() -> Any:
    """List all saved lead intelligence JSON result files, newest first."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        _OUTPUT_DIR.glob("*_lead_intelligence.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    entries = []
    for f in files:
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "run_id":    meta.get("run_id", f.stem),
                "total":     meta.get("total", 0),
                "enriched":  meta.get("enriched", 0),
                "high":      meta.get("high", 0),
                "medium":    meta.get("medium", 0),
                "low":       meta.get("low", 0),
                "json_path": str(f.resolve()),
            })
        except Exception:
            entries.append({"file": str(f), "error": "Could not parse"})
    return {"total_runs": len(entries), "runs": entries}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /prospect-results/{run_id}
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/prospect-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_prospect_result(run_id: str) -> Any:
    """Retrieve the full JSON output of one lead intelligence run."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _OUTPUT_DIR / f"{run_id}_lead_intelligence.json"
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
