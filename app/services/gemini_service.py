"""
Anthropic Claude service — structured job-description extraction.

Previously powered by Google Gemini; now replaced with the Anthropic Claude API.

Design decisions
────────────────
• The AsyncAnthropic client is built ONCE per api_key and cached at module
  level via `_client_cache`.  FastAPI creates a new GeminiService instance
  on every request (through Depends), but the expensive SDK client is shared,
  so there is no per-request connection overhead.

• The async Anthropic SDK client is used natively — no asyncio.to_thread needed.

• Claude's tool_use feature enforces the output schema, guaranteeing a valid
  JSON dict on every successful response (equivalent to Gemini's response_schema).

• Token counts are read from response.usage, so there is no async race condition.

• Retry logic (tenacity) handles transient 429 / 500 / connection errors with
  exponential back-off, up to 3 attempts.

Note: The class is still named GeminiService for backward-compatibility — all
existing imports in routes and services continue to work unchanged.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import anthropic
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.models.job_parser import (
    CLAUDE_JOB_PARSER_TOOL,
    BatchParseResponse,
    EmploymentType,
    JobParseResponse,
    ParsedJobDescription,
    SalaryPeriod,
    SalaryRange,
    SkillSet,
    WorkMode,
)

logger = structlog.get_logger(__name__)

# ── Module-level client cache (api_key → AsyncAnthropic) ──────────────────────
_client_cache: dict[str, anthropic.AsyncAnthropic] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Retry sentinel
# ══════════════════════════════════════════════════════════════════════════════

class _RetryableError(Exception):
    """Sentinel: tenacity retries on this; other exceptions propagate."""


# ══════════════════════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_INSTRUCTION = """\
You are an expert HR data analyst. Extract structured information from raw job descriptions.

Rules
─────
1. Only extract what is EXPLICITLY stated or strongly implied — never invent data.
2. Normalise skill names to their canonical form:
     "JS" → "JavaScript",  "k8s" → "Kubernetes",  "PG" → "PostgreSQL"
3. Salary: convert to numeric range with currency (ISO-4217) and period.
     "£500pd" → salary_min=500, salary_currency="GBP", salary_period="daily"
4. work_mode:
     fully remote → "remote"
     office + home split → "hybrid"
     office only → "onsite"
     unclear → "not_specified"
5. employment_type:
     "contract" / "outside IR35" / "day rate" → "contract"
     "permanent" / "perm" / "full-time" → "permanent"
     unknown → "not_specified"
6. confidence_score (0.0–1.0):
     1.0 = all important fields found and unambiguous
     0.5 = several fields missing or inferred
     0.0 = almost no structured data found
"""

_USER_TEMPLATE = """\
Extract structured data from the job description below.

=== JOB DESCRIPTION START ===
{description}
=== JOB DESCRIPTION END ===
"""


# ══════════════════════════════════════════════════════════════════════════════
# GeminiService  (name kept for backward-compatibility with existing imports)
# ══════════════════════════════════════════════════════════════════════════════

class GeminiService:
    """
    Thin async wrapper around the Anthropic Claude API.

    The class name is preserved for backward-compatibility — all routes and
    services that import GeminiService or call get_gemini() work unchanged.

    The heavy AsyncAnthropic client is cached at module level so it is built
    once and reused across every FastAPI request.
    """

    def __init__(self, api_key: str, model_name: str = "claude-sonnet-4-6") -> None:
        self._api_key    = api_key
        self._model_name = model_name
        self._client     = _get_or_create_client(api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    async def parse_job_description(self, description: str) -> JobParseResponse:
        """
        Send *description* to Claude and return a fully-typed JobParseResponse.

        Claude's tool_use feature enforces the response schema so we always
        receive a valid structured dict — no JSON parsing of raw text needed.
        """
        prompt = _USER_TEMPLATE.format(description=description)
        t0     = time.perf_counter()

        raw, tokens = await self._call_claude(prompt)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        parsed     = _map_to_model(raw)

        logger.info(
            "claude_parse_done",
            model      = self._model_name,
            elapsed_ms = elapsed_ms,
            tokens     = tokens,
            confidence = parsed.confidence_score,
        )

        return JobParseResponse(
            parsed             = parsed,
            model_used         = self._model_name,
            input_chars        = len(description),
            total_tokens       = tokens,
            processing_time_ms = elapsed_ms,
        )

    async def parse_batch(
        self,
        descriptions: list[str],
    ) -> BatchParseResponse:
        """Parse up to 10 descriptions concurrently."""
        t0 = time.perf_counter()

        results = await asyncio.gather(
            *[self.parse_job_description(d) for d in descriptions],
            return_exceptions=False,
        )

        total_tokens = sum(r.total_tokens or 0 for r in results) or None

        return BatchParseResponse(
            results            = list(results),
            total              = len(results),
            total_tokens       = total_tokens,
            processing_time_ms = round((time.perf_counter() - t0) * 1000, 1),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    @retry(
        retry   = retry_if_exception_type(_RetryableError),
        stop    = stop_after_attempt(3),
        wait    = wait_exponential(multiplier=1, min=2, max=10),
        reraise = True,
    )
    async def _call_claude(self, prompt: str) -> tuple[dict[str, Any], int | None]:
        """
        Async Claude API call using tool_use for guaranteed structured output.

        Returns
        ───────
        (input_dict, total_tokens)
            input_dict   – validated dict from the tool_use block
            total_tokens – prompt + completion tokens, or None if unavailable
        """
        try:
            response = await self._client.messages.create(
                model       = self._model_name,
                max_tokens  = 2048,
                system      = _SYSTEM_INSTRUCTION,
                tools       = [CLAUDE_JOB_PARSER_TOOL],
                tool_choice = {"type": "tool", "name": "extract_job_info"},
                messages    = [{"role": "user", "content": prompt}],
            )

            tokens = _extract_tokens(response)

            # Find the tool_use block in the response content
            tool_block = next(
                (b for b in response.content if b.type == "tool_use"),
                None,
            )
            if tool_block is None:
                raise ValueError("Claude did not return a tool_use block")

            return tool_block.input, tokens

        except anthropic.RateLimitError as exc:
            logger.warning("claude_rate_limit", error=str(exc))
            raise _RetryableError(str(exc)) from exc

        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                logger.error("claude_server_error", status=exc.status_code, error=str(exc))
                raise _RetryableError(str(exc)) from exc
            if exc.status_code in (401, 403):
                logger.error("claude_auth_error", status=exc.status_code)
                raise  # authentication errors must not be retried
            logger.error("claude_api_error", status=exc.status_code, error=str(exc))
            raise _RetryableError(str(exc)) from exc

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            logger.warning("claude_connection_error", error=str(exc))
            raise _RetryableError(str(exc)) from exc

        except ValueError:
            raise  # propagate our own ValueError (missing tool_use block)

        except Exception as exc:
            logger.error("claude_unexpected_error", error=str(exc))
            raise _RetryableError(str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_create_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Return a cached AsyncAnthropic client, building it once per api_key."""
    if api_key not in _client_cache:
        _client_cache[api_key] = anthropic.AsyncAnthropic(api_key=api_key)
        logger.info("claude_client_built")
    return _client_cache[api_key]


def _extract_tokens(response: Any) -> int | None:
    """Safely pull token usage from a Claude response object."""
    try:
        usage = response.usage
        if usage:
            return int(usage.input_tokens + usage.output_tokens)
    except Exception:
        pass
    return None


def _safe_enum(enum_cls: type, value: Any, default: Any) -> Any:
    """Coerce *value* into *enum_cls*, falling back to *default*."""
    try:
        return enum_cls(value) if value else default
    except ValueError:
        return default


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _map_to_model(raw: dict[str, Any]) -> ParsedJobDescription:
    """Map the flat Claude tool_use JSON response to the nested ParsedJobDescription."""

    # ── Salary ────────────────────────────────────────────────────────────────
    salary: SalaryRange | None = None
    if raw.get("salary_min") is not None or raw.get("salary_max") is not None:
        salary = SalaryRange(
            min_value = raw.get("salary_min"),
            max_value = raw.get("salary_max"),
            currency  = raw.get("salary_currency") or "USD",
            period    = _safe_enum(SalaryPeriod, raw.get("salary_period"), SalaryPeriod.ANNUAL),
            raw_text  = raw.get("salary_raw_text") or None,
        )

    # ── Skills ────────────────────────────────────────────────────────────────
    skills = SkillSet(
        required  = raw.get("required_skills")  or [],
        preferred = raw.get("preferred_skills") or [],
    )

    return ParsedJobDescription(
        job_title              = raw.get("job_title")              or None,
        company_name           = raw.get("company_name")           or None,
        skills                 = skills,
        location               = raw.get("location")               or None,
        salary                 = salary,
        work_mode              = _safe_enum(WorkMode,       raw.get("work_mode"),       WorkMode.NOT_SPECIFIED),
        employment_type        = _safe_enum(EmploymentType, raw.get("employment_type"), EmploymentType.NOT_SPECIFIED),
        experience_years_min   = _safe_int(raw.get("experience_years_min")),
        experience_years_max   = _safe_int(raw.get("experience_years_max")),
        education_requirement  = raw.get("education_requirement") or None,
        languages              = raw.get("languages")  or [],
        benefits               = raw.get("benefits")   or [],
        confidence_score       = float(raw.get("confidence_score") or 0.0),
        extraction_notes       = raw.get("extraction_notes")       or None,
    )
