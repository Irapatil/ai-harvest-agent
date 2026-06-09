"""
Job-description parsing endpoints — powered by Anthropic Claude.

Routes
──────
  POST /api/v1/jobs/parse           parse one job description
  POST /api/v1/jobs/parse/batch     parse up to 10 descriptions concurrently

Authentication
──────────────
  All routes require  X-API-Key: <value>  (configured via API_KEY env var).

Claude singleton
─────────────────
  get_gemini()  is a cached FastAPI dependency.  The first call builds the SDK
  client object; every subsequent call within the process returns the same
  instance.  This avoids rebuilding the client on every HTTP request.
"""
from __future__ import annotations

import time
from functools import lru_cache

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.core.exceptions import LLMError
from app.core.security import require_api_key
from app.models.job_parser import (
    BatchParseRequest,
    BatchParseResponse,
    JobParseRequest,
    JobParseResponse,
)
from app.models.response import APIResponse
from app.services.gemini_service import GeminiService

logger = structlog.get_logger(__name__)

router = APIRouter(
    dependencies=[Depends(require_api_key)],
    tags=["Job Parser"],
)


# ══════════════════════════════════════════════════════════════════════════════
# Dependency — singleton GeminiService
# ══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _build_gemini_service(api_key: str, model_name: str) -> GeminiService:
    """
    Build the GeminiService once per (api_key, model_name) pair.
    lru_cache ensures the expensive SDK model object is created only once.
    """
    return GeminiService(api_key=api_key, model_name=model_name)


def get_gemini(settings: Settings = Depends(get_settings)) -> GeminiService:
    """FastAPI dependency that provides a cached GeminiService instance."""
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ANTHROPIC_API_KEY is not set on this server. "
                "Add it to your .env file and restart."
            ),
        )
    return _build_gemini_service(
        api_key    = settings.anthropic_api_key,
        model_name = settings.anthropic_model,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse  — single job description
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/parse",
    response_model=APIResponse[JobParseResponse],
    status_code=status.HTTP_200_OK,
    summary="Parse a raw job description",
    description="""
Submit the raw text of any job posting. Claude extracts and returns:

| Field | Description |
|---|---|
| `skills.required` | Must-have technical & soft skills (normalised) |
| `skills.preferred` | Nice-to-have skills |
| `location` | City / country, or `"Remote"` |
| `work_mode` | `remote` · `hybrid` · `onsite` · `not_specified` |
| `salary` | `{min, max, currency, period, raw_text}` |
| `employment_type` | `contract` · `permanent` · `part_time` · `freelance` |
| `experience_years_min/max` | Required years of experience |
| `education_requirement` | Degree / certification requirement |
| `benefits` | Listed perks |
| `confidence_score` | 0–1 completeness rating assigned by the model |

**Authentication:** `X-API-Key` header required.
""",
    responses={
        200: {"description": "Parsed successfully"},
        401: {"description": "Missing or invalid X-API-Key"},
        422: {"description": "Description too short (< 50 chars) or too long (> 20 000 chars)"},
        502: {"description": "Claude returned an unparseable response"},
        503: {"description": "ANTHROPIC_API_KEY not configured, or Claude quota exceeded"},
    },
)
async def parse_job_description(
    body:   JobParseRequest,
    gemini: GeminiService = Depends(get_gemini),
) -> APIResponse[JobParseResponse]:

    log = logger.bind(input_chars=len(body.description))
    log.info("job_parse_requested")

    result = await _run_parse(gemini.parse_job_description, body.description, log)

    log.info(
        "job_parse_done",
        model      = result.model_used,
        elapsed_ms = result.processing_time_ms,
        confidence = result.parsed.confidence_score,
        tokens     = result.total_tokens,
    )
    return APIResponse(data=result, message="Job description parsed successfully")


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse/batch  — up to 10 descriptions concurrently
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/parse/batch",
    response_model=APIResponse[BatchParseResponse],
    status_code=status.HTTP_200_OK,
    summary="Parse up to 10 job descriptions in one call",
    description="""
Send a list of 1–10 raw job descriptions. All are parsed concurrently via
`asyncio.gather`, so latency ≈ the slowest single call rather than the sum.

Each result in `data.results` has the same shape as the single `/parse` endpoint.

**Authentication:** `X-API-Key` header required.
""",
    responses={
        200: {"description": "All descriptions parsed"},
        401: {"description": "Missing or invalid X-API-Key"},
        422: {"description": "Validation error (> 10 items, or an item < 50 chars)"},
        503: {"description": "Claude not configured"},
    },
)
async def parse_batch(
    body:   BatchParseRequest,
    gemini: GeminiService = Depends(get_gemini),
) -> APIResponse[BatchParseResponse]:

    count = len(body.descriptions)
    log   = logger.bind(batch_size=count)
    log.info("job_parse_batch_requested")

    try:
        result = await gemini.parse_batch(body.descriptions)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Claude returned an unparseable response: {exc}",
        ) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        log.exception("job_parse_batch_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error while calling Claude.",
        ) from exc

    log.info(
        "job_parse_batch_done",
        elapsed_ms   = result.processing_time_ms,
        total_tokens = result.total_tokens,
    )
    return APIResponse(
        data    = result,
        message = f"Parsed {result.total} job description(s) successfully",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Shared error-handling helper
# ══════════════════════════════════════════════════════════════════════════════

async def _run_parse(coro_fn, description: str, log) -> JobParseResponse:
    """
    Call *coro_fn(description)* and map exceptions to HTTP errors.
    Extracted so single and batch routes share identical error handling.
    """
    try:
        return await coro_fn(description)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Claude returned an unparseable response: {exc}",
        ) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        log.exception("job_parse_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error while calling Claude.",
        ) from exc
