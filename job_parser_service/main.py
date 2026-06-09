"""
Job Parser Service — FastAPI application.

Endpoints
─────────
  GET  /health              liveness + Claude connectivity check
  POST /parse               parse one job description
  POST /parse/batch         parse up to 10 descriptions concurrently

Auth
────
  All routes (except /health) require header:
      X-API-Key: <value of API_KEY env var>
  Set API_KEY="" to disable auth (development only).

Start
─────
  uvicorn job_parser_service.main:app --reload --port 8001
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from job_parser_service.config import Settings, get_settings
from job_parser_service.gemini import GeminiService
from job_parser_service.models import (
    APIResponse,
    BatchParseRequest,
    BatchParseResponse,
    JobParseRequest,
    JobParseResponse,
    ParsedJobDescription,
)

logger = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Gemini singleton dependency
# ══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _build_gemini(api_key: str, model: str, retries: int) -> GeminiService:
    """Build GeminiService once; lru_cache prevents re-construction."""
    return GeminiService(api_key=api_key, model_name=model, max_retries=retries)


def get_gemini(settings: Settings = Depends(get_settings)) -> GeminiService:
    """FastAPI dependency — returns the shared, cached GeminiService (Claude-backed)."""
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured. Set it in .env and restart.",
        )
    return _build_gemini(
        api_key = settings.anthropic_api_key,
        model   = settings.anthropic_model,
        retries = settings.anthropic_max_retries,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Auth dependency
# ══════════════════════════════════════════════════════════════════════════════

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    provided: str | None = Security(_API_KEY_HEADER),
    settings: Settings   = Depends(get_settings),
) -> None:
    """Validate X-API-Key header. No-op when API_KEY is empty (auth disabled)."""
    if not settings.auth_enabled:
        return
    if provided is None or provided != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ══════════════════════════════════════════════════════════════════════════════
# Error mapper
# ══════════════════════════════════════════════════════════════════════════════

async def _parse(coro, log: structlog.BoundLogger) -> JobParseResponse:
    """Run a parse coroutine; map exceptions to typed HTTP responses."""
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail      = f"Claude returned an unparseable response: {exc}",
        ) from exc
    except Exception as exc:
        err = str(exc).lower()
        if "quota" in err or "429" in err or "rate_limit" in err:
            raise HTTPException(
                status_code = status.HTTP_429_TOO_MANY_REQUESTS,
                detail      = "Claude rate limit exceeded — retry after a short delay.",
            ) from exc
        if any(k in err for k in ("api_key", "401", "403", "permission", "authentication")):
            raise HTTPException(
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
                detail      = "Claude authentication error — check ANTHROPIC_API_KEY.",
            ) from exc
        log.exception("parse_error", error=str(exc))
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Unexpected error while calling Claude.",
        ) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════════

router = APIRouter(dependencies=[Depends(require_api_key)])


# ── GET /health ───────────────────────────────────────────────────────────────

@router.get(
    "/health",
    tags=["Health"],
    summary="Service liveness and Claude connectivity check",
    dependencies=[],   # health check is unauthenticated
)
async def health(settings: Settings = Depends(get_settings)) -> JSONResponse:
    claude_ok = bool(settings.anthropic_api_key)
    return JSONResponse(
        status_code = 200 if claude_ok else 503,
        content     = {
            "status":          "ok" if claude_ok else "degraded",
            "service":         settings.service_name,
            "version":         settings.service_version,
            "environment":     settings.environment,
            "claude_model":    settings.anthropic_model,
            "claude_key_set":  claude_ok,
        },
    )


# ── POST /parse ───────────────────────────────────────────────────────────────

@router.post(
    "/parse",
    response_model    = APIResponse[JobParseResponse],
    status_code       = status.HTTP_200_OK,
    tags              = ["Parsing"],
    summary           = "Parse a raw job description",
    description       = """
Submit raw job description text. **Claude** extracts and returns structured JSON:

| Field | Type | Description |
|---|---|---|
| `skills.required` | `string[]` | Must-have technical & soft skills (normalised) |
| `skills.preferred` | `string[]` | Nice-to-have skills |
| `location` | `string\\|null` | City / country or `"Remote"` |
| `work_mode` | enum | `remote` · `hybrid` · `onsite` · `not_specified` |
| `salary` | object\\|null | `{min, max, currency, period, raw_text}` |
| `employment_type` | enum | `contract` · `permanent` · `part_time` · `freelance` |
| `experience_years_min/max` | `int\\|null` | Years of experience required |
| `education_requirement` | `string\\|null` | Degree / certification |
| `benefits` | `string[]` | Listed perks |
| `confidence_score` | `float` | 0–1 completeness score |

**Auth:** `X-API-Key` header (omit if `API_KEY` env var is empty).
""",
    responses={
        200: {"description": "Parsed successfully"},
        401: {"description": "Invalid or missing X-API-Key"},
        422: {"description": "Description < 50 or > 20 000 characters"},
        429: {"description": "Claude rate limit exceeded"},
        502: {"description": "Claude returned an invalid response"},
        503: {"description": "ANTHROPIC_API_KEY not set or auth failure"},
    },
)
async def parse_one(
    body:   JobParseRequest,
    request: Request,
    gemini: GeminiService = Depends(get_gemini),
) -> APIResponse[JobParseResponse]:
    rid = request.headers.get("X-Request-Id", str(uuid.uuid4())[:8])
    log = logger.bind(request_id=rid, chars=len(body.description))
    log.info("parse_requested")

    result = await _parse(gemini.parse(body.description), log)

    log.info(
        "parse_done",
        ms         = result.processing_time_ms,
        tokens     = result.total_tokens,
        confidence = result.parsed.confidence_score,
    )
    return APIResponse(data=result, message="Parsed successfully")


# ── POST /parse/batch ─────────────────────────────────────────────────────────

@router.post(
    "/parse/batch",
    response_model    = APIResponse[BatchParseResponse],
    status_code       = status.HTTP_200_OK,
    tags              = ["Parsing"],
    summary           = "Parse up to 10 descriptions concurrently",
    description       = """
Send 1–10 raw job descriptions in one call. All are parsed **concurrently**
via `asyncio.gather`, so latency ≈ the slowest single call, not the sum.

Each item in `data.results` has the same shape as the single `/parse` response.

**Auth:** `X-API-Key` header.
""",
    responses={
        200: {"description": "All descriptions parsed"},
        401: {"description": "Invalid or missing X-API-Key"},
        422: {"description": "Validation error — > 10 items, or an item < 50 chars"},
        503: {"description": "Claude not configured"},
    },
)
async def parse_batch(
    body:   BatchParseRequest,
    request: Request,
    gemini: GeminiService = Depends(get_gemini),
) -> APIResponse[BatchParseResponse]:
    rid = request.headers.get("X-Request-Id", str(uuid.uuid4())[:8])
    log = logger.bind(request_id=rid, batch_size=len(body.descriptions))
    log.info("batch_parse_requested")

    try:
        result = await gemini.parse_many(body.descriptions)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("batch_parse_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error during batch parse.",
        ) from exc

    log.info("batch_parse_done", ms=result.processing_time_ms, tokens=result.total_tokens)
    return APIResponse(
        data    = result,
        message = f"Parsed {result.total} description(s) successfully",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Middleware
# ══════════════════════════════════════════════════════════════════════════════

class _RequestLogger:
    """Log every request: method, path, status, duration."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0     = time.perf_counter()
        status_code = 0

        async def _send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        request = Request(scope, receive)
        await self.app(scope, receive, _send)

        logger.info(
            "http_request",
            method   = request.method,
            path     = request.url.path,
            status   = status_code,
            duration = round((time.perf_counter() - t0) * 1000, 1),
        )


# ══════════════════════════════════════════════════════════════════════════════
# App factory
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    cfg = get_settings()
    logger.info(
        "service_starting",
        service = cfg.service_name,
        version = cfg.service_version,
        env     = cfg.environment,
        model   = cfg.anthropic_model,
        auth    = cfg.auth_enabled,
    )
    yield
    logger.info("service_stopping")


def create_app() -> FastAPI:
    cfg = get_settings()

    application = FastAPI(
        title       = "Job Parser Service",
        description = (
            "Extract structured information from raw job descriptions using "
            "Anthropic Claude with tool_use for enforced JSON output."
        ),
        version      = cfg.service_version,
        docs_url     = "/docs",
        redoc_url    = "/redoc",
        openapi_url  = "/openapi.json",
        lifespan     = _lifespan,
    )

    # CORS
    application.add_middleware(
        CORSMiddleware,
        allow_origins     = cfg.cors_origins,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # Request logging
    application.add_middleware(_RequestLogger)

    # Routes
    application.include_router(router)

    return application


app = create_app()
