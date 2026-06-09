"""
Anthropic Claude wrapper for structured job-description extraction.

Previously powered by Google Gemini — now replaced with the Anthropic Claude API.

Key design choices
──────────────────
1. Module-level client cache
   AsyncAnthropic is expensive to build. _CLIENT_CACHE[api_key] keeps one
   instance per unique key, shared across every FastAPI request — no
   per-request overhead.

2. Native async
   The Anthropic SDK ships an AsyncAnthropic client. We use it directly,
   so no asyncio.to_thread offloading is needed.

3. Structured output via tool_use
   Claude's tool_use feature enforces the output schema and always returns a
   valid JSON dict — equivalent to Gemini's response_schema enforcement.
   Tokens are read from response.usage (no async race condition).

4. Tenacity retry
   Transient RateLimitError / server 5xx / connection errors are retried up to
   anthropic_max_retries times with exponential back-off.
   Auth errors (401/403) raise immediately.

5. Few-shot system instruction
   Three concrete examples anchor the extraction format and edge-case handling
   (salary normalisation, skill canonicalisation, etc.).

Note: The class is still named GeminiService for backward-compatibility — all
existing imports in main.py and tests continue to work unchanged.
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

from job_parser_service.models import (
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

# ── Module-level client cache ──────────────────────────────────────────────────
_CLIENT_CACHE: dict[str, anthropic.AsyncAnthropic] = {}

# ══════════════════════════════════════════════════════════════════════════════
# Retry sentinel
# ══════════════════════════════════════════════════════════════════════════════

class _RetryableError(Exception):
    """Sentinel: tenacity retries on this; other exceptions propagate."""


# ══════════════════════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM = """\
You are an expert HR data analyst. Extract structured information from job descriptions.

## Rules

1. Extract ONLY what is explicitly stated or strongly implied. Never fabricate.
2. Normalise skill names to canonical form:
   • "JS"   → "JavaScript"
   • "k8s"  → "Kubernetes"
   • "PG"   → "PostgreSQL"
   • "py"   → "Python"
3. Salary — always convert to a numeric range with ISO-4217 currency + period:
   • "£500pd"      → salary_min=500, salary_currency="GBP", salary_period="daily"
   • "$80–100k pa" → salary_min=80000, salary_max=100000, salary_currency="USD", salary_period="annual"
4. work_mode:
   • Fully remote / "work from anywhere"  → "remote"
   • Office + home ("2 days in office")   → "hybrid"
   • On-site / office-only                → "onsite"
   • Unclear / not mentioned              → "not_specified"
5. employment_type:
   • "contract" / "outside IR35" / "day rate" / "freelance"   → "contract"
   • "permanent" / "perm" / "full-time" / "FTE"               → "permanent"
   • "part-time" / "part time"                                 → "part_time"
   • Internship / graduate / placement                         → "internship"
   • Unclear                                                   → "not_specified"
6. confidence_score (0.0–1.0):
   • 1.0 — all key fields present and unambiguous
   • 0.7 — most fields found; a few missing or inferred
   • 0.4 — partial data; many fields missing
   • 0.0 — almost no structured data found

## Examples

### Input
We are looking for a Senior Java Developer on a 6-month contract basis.
The role is fully remote. Rate: £500–£650 per day (outside IR35).
Required: Java 17, Spring Boot 3, REST APIs, Docker. Nice to have: Kafka, AWS.
5+ years experience. Benefits: 25 days holiday, pension.

### Expected output (via extract_job_info tool)
{
  "job_title": "Senior Java Developer",
  "company_name": null,
  "required_skills": ["Java 17", "Spring Boot 3", "REST APIs", "Docker"],
  "preferred_skills": ["Kafka", "AWS"],
  "location": null,
  "salary_min": 500,
  "salary_max": 650,
  "salary_currency": "GBP",
  "salary_period": "daily",
  "salary_raw_text": "£500–£650 per day",
  "work_mode": "remote",
  "employment_type": "contract",
  "experience_years_min": 5,
  "experience_years_max": null,
  "education_requirement": null,
  "languages": [],
  "benefits": ["25 days holiday", "pension"],
  "confidence_score": 0.92,
  "extraction_notes": null
}

### Input
Join our London office as a permanent Python Backend Engineer. Hybrid (3 days in).
Salary: $110,000–$130,000 per year + bonus + equity.
Must have: Python, FastAPI, PostgreSQL, AWS. Desirable: Redis, Kafka.
Bachelor's degree in Computer Science or related field preferred.

### Expected output
{
  "job_title": "Python Backend Engineer",
  "company_name": null,
  "required_skills": ["Python", "FastAPI", "PostgreSQL", "AWS"],
  "preferred_skills": ["Redis", "Kafka"],
  "location": "London",
  "salary_min": 110000,
  "salary_max": 130000,
  "salary_currency": "USD",
  "salary_period": "annual",
  "salary_raw_text": "$110,000–$130,000 per year",
  "work_mode": "hybrid",
  "employment_type": "permanent",
  "experience_years_min": null,
  "experience_years_max": null,
  "education_requirement": "Bachelor's degree in Computer Science or related field",
  "languages": [],
  "benefits": ["bonus", "equity"],
  "confidence_score": 0.88,
  "extraction_notes": "Degree listed as 'preferred' not required — flagged"
}

### Input
We need someone who knows some web stuff. Apply now!

### Expected output
{
  "job_title": null,
  "company_name": null,
  "required_skills": [],
  "preferred_skills": [],
  "location": null,
  "salary_min": null,
  "salary_max": null,
  "salary_currency": "USD",
  "salary_period": "annual",
  "salary_raw_text": null,
  "work_mode": "not_specified",
  "employment_type": "not_specified",
  "experience_years_min": null,
  "experience_years_max": null,
  "education_requirement": null,
  "languages": [],
  "benefits": [],
  "confidence_score": 0.05,
  "extraction_notes": "Job description contains almost no structured information"
}
"""

_USER_TEMPLATE = """\
Extract structured data from the job description below.

=== JOB DESCRIPTION ===
{description}
=== END ===
"""


# ══════════════════════════════════════════════════════════════════════════════
# GeminiService  (name kept for backward-compatibility with existing imports)
# ══════════════════════════════════════════════════════════════════════════════

class GeminiService:
    """
    Async Claude wrapper.  Constructed once and reused via the FastAPI
    dependency cache (_build_service in main.py).

    The class name is preserved for backward-compatibility — test files and
    main.py that import GeminiService continue to work unchanged.
    """

    def __init__(
        self,
        api_key:     str,
        model_name:  str = "claude-sonnet-4-6",
        max_retries: int = 3,
    ) -> None:
        self._model_name  = model_name
        self._max_retries = max_retries
        self._client      = _get_or_build_client(api_key)

    # ── Public ────────────────────────────────────────────────────────────────

    async def parse(self, description: str) -> JobParseResponse:
        """
        Send *description* to Claude and return a typed JobParseResponse.

        Uses tool_use so the response is always a validated JSON dict —
        no raw-text JSON parsing needed.
        """
        prompt = _USER_TEMPLATE.format(description=description)
        t0     = time.perf_counter()

        raw, tokens = await self._call(prompt)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        parsed     = _map(raw)

        logger.info(
            "claude_parsed",
            model      = self._model_name,
            elapsed_ms = elapsed_ms,
            tokens     = tokens,
            confidence = parsed.confidence_score,
            skills     = len(parsed.skills.required),
        )

        return JobParseResponse(
            parsed             = parsed,
            model_used         = self._model_name,
            input_chars        = len(description),
            total_tokens       = tokens,
            processing_time_ms = elapsed_ms,
        )

    async def parse_many(self, descriptions: list[str]) -> BatchParseResponse:
        """Parse multiple descriptions concurrently."""
        t0      = time.perf_counter()
        results = list(await asyncio.gather(*[self.parse(d) for d in descriptions]))
        tokens  = sum(r.total_tokens or 0 for r in results) or None

        return BatchParseResponse(
            results            = results,
            total              = len(results),
            total_tokens       = tokens,
            processing_time_ms = round((time.perf_counter() - t0) * 1000, 1),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _call(self, prompt: str) -> tuple[dict[str, Any], int | None]:
        """Dispatch to the tenacity-retried inner call."""
        return await self._call_with_retry(prompt)

    async def _call_with_retry(self, prompt: str) -> tuple[dict[str, Any], int | None]:
        """Build a tenacity-retried coroutine on the fly."""
        max_r = self._max_retries

        @retry(
            retry   = retry_if_exception_type(_RetryableError),
            stop    = stop_after_attempt(max_r),
            wait    = wait_exponential(multiplier=1, min=2, max=10),
            reraise = True,
        )
        async def _inner(p: str) -> tuple[dict[str, Any], int | None]:
            return await self._call_once(p)

        return await _inner(prompt)

    async def _call_once(self, prompt: str) -> tuple[dict[str, Any], int | None]:
        try:
            response = await self._client.messages.create(
                model       = self._model_name,
                max_tokens  = 2048,
                system      = _SYSTEM,
                tools       = [CLAUDE_JOB_PARSER_TOOL],
                tool_choice = {"type": "tool", "name": "extract_job_info"},
                messages    = [{"role": "user", "content": prompt}],
            )

            tokens = _read_tokens(response)

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
                raise  # must not be retried
            logger.error("claude_api_error", status=exc.status_code, error=str(exc))
            raise _RetryableError(str(exc)) from exc

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            logger.warning("claude_connection_error", error=str(exc))
            raise _RetryableError(str(exc)) from exc

        except ValueError:
            raise  # our own "no tool_use block" error

        except Exception as exc:
            logger.error("claude_unexpected_error", error=str(exc))
            raise _RetryableError(str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_build_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Return a cached AsyncAnthropic client, building it once per api_key."""
    if api_key not in _CLIENT_CACHE:
        _CLIENT_CACHE[api_key] = anthropic.AsyncAnthropic(api_key=api_key)
        logger.info("claude_client_built")
    return _CLIENT_CACHE[api_key]


def _read_tokens(response: Any) -> int | None:
    try:
        u = response.usage
        return int(u.input_tokens + u.output_tokens) if u else None
    except Exception:
        return None


def _safe_enum(cls, value: Any, default: Any) -> Any:
    try:
        return cls(value) if value else default
    except ValueError:
        return default


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _map(raw: dict[str, Any]) -> ParsedJobDescription:
    """Map the flat Claude tool_use JSON dict to the nested ParsedJobDescription."""

    salary: SalaryRange | None = None
    if raw.get("salary_min") is not None or raw.get("salary_max") is not None:
        salary = SalaryRange(
            min_value = raw.get("salary_min"),
            max_value = raw.get("salary_max"),
            currency  = raw.get("salary_currency") or "USD",
            period    = _safe_enum(SalaryPeriod, raw.get("salary_period"), SalaryPeriod.ANNUAL),
            raw_text  = raw.get("salary_raw_text") or None,
        )

    return ParsedJobDescription(
        job_title              = raw.get("job_title")             or None,
        company_name           = raw.get("company_name")          or None,
        skills                 = SkillSet(
            required  = raw.get("required_skills")  or [],
            preferred = raw.get("preferred_skills") or [],
        ),
        location               = raw.get("location")              or None,
        salary                 = salary,
        work_mode              = _safe_enum(WorkMode,       raw.get("work_mode"),       WorkMode.NOT_SPECIFIED),
        employment_type        = _safe_enum(EmploymentType, raw.get("employment_type"), EmploymentType.NOT_SPECIFIED),
        experience_years_min   = _safe_int(raw.get("experience_years_min")),
        experience_years_max   = _safe_int(raw.get("experience_years_max")),
        education_requirement  = raw.get("education_requirement") or None,
        languages              = raw.get("languages")  or [],
        benefits               = raw.get("benefits")   or [],
        confidence_score       = float(raw.get("confidence_score") or 0.0),
        extraction_notes       = raw.get("extraction_notes")      or None,
    )
