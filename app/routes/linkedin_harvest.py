"""
LinkedIn harvest endpoints.

Routes
──────
  POST /api/v1/jobs/linkedin/search         Phase 1 only — raw cards, no Gemini cost
  POST /api/v1/jobs/linkedin/harvest        Full 3-phase pipeline (sync, ~90 s)
  POST /api/v1/jobs/linkedin/harvest/async  Start harvest as background task
  GET  /api/v1/jobs/linkedin/harvest/{id}   Poll background task status + result

Dependency design
─────────────────
  get_gemini()         (from job_parser.py)  — shared, lru-cached GeminiService singleton
  get_pipeline()       — injects the cached Gemini into LinkedInPipelineService
  get_pipeline_search_only()  — injects gemini=None for search-only calls

This ensures the expensive Gemini SDK model object is built once per process,
regardless of whether the request came from the job-parser endpoint or the
LinkedIn harvest endpoint.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.core.security import require_api_key
from app.models.linkedin import (
    EnrichedLinkedInJob,
    HarvestJob,
    HarvestJobSummary,
    HarvestStatus,
    LinkedInHarvestResult,
    LinkedInSearchConfig,
    LinkedInSearchResult,
)
from app.models.response import APIResponse
from app.routes.job_parser import get_gemini          # ← shared singleton
from app.services.gemini_service import GeminiService
from app.services.linkedin_pipeline import LinkedInPipelineService

logger = structlog.get_logger(__name__)

router = APIRouter(
    dependencies=[Depends(require_api_key)],
    tags=["LinkedIn Harvest"],
)

# ── In-memory harvest-job store ───────────────────────────────────────────────
# Sufficient for development / single-process deployments.
# Replace with Redis (via aioredis) for multi-process / multi-node production.
_harvest_jobs: dict[str, HarvestJob] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Dependencies
# ══════════════════════════════════════════════════════════════════════════════

def get_pipeline(
    settings: Settings             = Depends(get_settings),
    gemini:   GeminiService        = Depends(get_gemini),   # shared singleton
) -> LinkedInPipelineService:
    """
    Full pipeline: Playwright search + description fetch + Gemini parse.
    Reuses the same GeminiService instance as the job-parser endpoint.
    """
    return LinkedInPipelineService(settings=settings, gemini=gemini)


def get_pipeline_search_only(
    settings: Settings = Depends(get_settings),
) -> LinkedInPipelineService:
    """
    Phase-1-only pipeline (no Gemini).
    GeminiService is intentionally omitted (defaults to None inside the service).
    No GEMINI_API_KEY required for this dependency.
    """
    return LinkedInPipelineService(settings=settings, gemini=None)


# ══════════════════════════════════════════════════════════════════════════════
# POST /search  — Phase 1 only (fast preview, no Gemini cost)
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/search",
    response_model=APIResponse[LinkedInSearchResult],
    status_code=status.HTTP_200_OK,
    summary="LinkedIn search preview — raw cards only",
    description="""
**Phase 1 only.** Playwright navigates LinkedIn Jobs and returns raw job cards
(title, company, location, URL).

No detail pages are visited and Gemini is **not** called, so this is fast
(~10 s) and free.  Use it to validate your search config before running the
full harvest.

`GEMINI_API_KEY` is **not** required for this endpoint.
""",
)
async def linkedin_search(
    config:   LinkedInSearchConfig,
    pipeline: LinkedInPipelineService = Depends(get_pipeline_search_only),
) -> APIResponse[LinkedInSearchResult]:

    log = logger.bind(keywords=config.keywords)
    log.info("linkedin_search_requested")

    try:
        result = await pipeline.search_only(config)
    except Exception as exc:
        log.exception("linkedin_search_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LinkedIn search failed: {exc}",
        ) from exc

    log.info("linkedin_search_done", found=result.total_found, ms=result.duration_ms)
    return APIResponse(
        data    = result,
        message = f"Found {result.total_found} jobs for '{config.keywords}'",
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /harvest  — full 3-phase pipeline (synchronous)
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/harvest",
    response_model=APIResponse[LinkedInHarvestResult],
    status_code=status.HTTP_200_OK,
    summary="LinkedIn → Gemini full harvest (sync, ~90 s)",
    description="""
Runs the complete 3-phase pipeline and **blocks until done**.

1. **Search** (Playwright) — collects job cards matching your filters.
2. **Describe** (Playwright) — opens each job-detail page to get the full JD text.
3. **Parse** (Gemini) — extracts skills, location, salary, work mode from each description.

> ⏱ Expect ~90 s for 25 jobs.  For non-blocking operation use `POST /harvest/async`.

Phases 2 + 3 run concurrently (bounded by `description_concurrency`) so the
wall-clock time ≈ slowest individual job × a few, not the sum of all jobs.

**Smart merge:** when Gemini cannot extract `location`, `job_title`, or
`company_name` from the description, the values from the LinkedIn search card
are backfilled automatically.  `effective_*` properties on each job reflect
the merged result.

`GEMINI_API_KEY` is required.  Use `POST /search` to preview without it.
""",
    responses={
        200: {"description": "Pipeline complete — check `errors` field for partial failures"},
        503: {"description": "GEMINI_API_KEY not configured"},
    },
)
async def linkedin_harvest(
    config:   LinkedInSearchConfig,
    pipeline: LinkedInPipelineService = Depends(get_pipeline),
) -> APIResponse[LinkedInHarvestResult]:

    log = logger.bind(keywords=config.keywords, max_jobs=config.max_jobs)
    log.info("linkedin_harvest_requested")

    try:
        result = await pipeline.harvest(config)
    except Exception as exc:
        log.exception("linkedin_harvest_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Pipeline failed: {exc}",
        ) from exc

    log.info(
        "linkedin_harvest_done",
        found    = result.total_found,
        described= result.total_described,
        parsed   = result.total_parsed,
        ms       = result.duration_ms,
        errors   = len(result.errors),
    )
    return APIResponse(
        data    = result,
        message = (
            f"Harvested {result.total_found} jobs "
            f"({result.total_described} described, {result.total_parsed} parsed)"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /harvest/async  — start harvest in background, return immediately
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/harvest/async",
    response_model=APIResponse[HarvestJobSummary],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start LinkedIn harvest as a background task",
    description="""
Enqueues the full 3-phase harvest pipeline as a background task and returns
immediately with a `job_id`.

Poll `GET /harvest/{job_id}` for status (`pending` → `running` → `done` / `failed`).
When status is `done`, the full `LinkedInHarvestResult` is included in the response.

```
POST /harvest/async  →  { "data": { "id": "abc-123", "status": "pending" } }
GET  /harvest/abc-123  →  { "data": { "status": "running", ... } }
GET  /harvest/abc-123  →  { "data": { "status": "done", "result": { ... } } }
```
""",
)
async def linkedin_harvest_async(
    config:           LinkedInSearchConfig,
    background_tasks: BackgroundTasks,
    pipeline:         LinkedInPipelineService = Depends(get_pipeline),
) -> APIResponse[HarvestJobSummary]:

    job = HarvestJob(config=config)
    _harvest_jobs[job.id] = job

    background_tasks.add_task(_run_harvest_background, job.id, config, pipeline)

    logger.info("linkedin_harvest_async_queued", job_id=job.id, keywords=config.keywords)
    return APIResponse(
        data    = HarvestJobSummary.from_job(job),
        message = f"Harvest job {job.id} queued. Poll GET /harvest/{job.id} for status.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest/{job_id}  — poll background task
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/harvest/{job_id}",
    response_model=APIResponse[HarvestJob],
    status_code=status.HTTP_200_OK,
    summary="Get background harvest job status and result",
    description="""
Returns the current status of a background harvest job.

| Status | Meaning |
|---|---|
| `pending` | Task is queued, not started yet |
| `running` | Playwright + Gemini pipeline is executing |
| `done` | Complete — `result` contains the full `LinkedInHarvestResult` |
| `failed` | Pipeline failed — `error` contains the reason |
""",
)
async def get_harvest_job(job_id: str) -> APIResponse[HarvestJob]:
    job = _harvest_jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Harvest job '{job_id}' not found.",
        )
    return APIResponse(data=job, message=f"Harvest job status: {job.status}")


# ══════════════════════════════════════════════════════════════════════════════
# Background task runner
# ══════════════════════════════════════════════════════════════════════════════

async def _run_harvest_background(
    job_id:   str,
    config:   LinkedInSearchConfig,
    pipeline: LinkedInPipelineService,
) -> None:
    """
    Runs inside FastAPI's BackgroundTasks executor.
    Updates _harvest_jobs[job_id] in-place as the pipeline progresses.
    """
    job = _harvest_jobs[job_id]
    job.status = HarvestStatus.RUNNING
    logger.info("harvest_background_start", job_id=job_id, keywords=config.keywords)

    try:
        result            = await pipeline.harvest(config)
        job.result        = result
        job.status        = HarvestStatus.DONE
        job.completed_at  = datetime.now(timezone.utc)
        logger.info(
            "harvest_background_done",
            job_id   = job_id,
            found    = result.total_found,
            parsed   = result.total_parsed,
            duration = result.duration_ms,
        )
    except Exception as exc:
        job.status       = HarvestStatus.FAILED
        job.error        = str(exc)
        job.completed_at = datetime.now(timezone.utc)
        logger.exception("harvest_background_failed", job_id=job_id, error=str(exc))
