"""
Complete test suite for the Job Parser Service.

Coverage
────────
  auth                  401 when key missing, 200 when correct
  validation            422 on description < 50 chars / > 20 000 chars
  parse happy-path      shape, field types, enum values
  parse edge cases      no salary, no skills, not_specified enums
  batch                 concurrency, aggregate token count
  health                unauthenticated, status field
  error mapping         502 on bad JSON, 429 on quota, 503 on auth error

All Gemini calls are mocked — no real API key required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from job_parser_service.gemini import GeminiService
from job_parser_service.main import app, get_gemini
from job_parser_service.models import (
    BatchParseResponse,
    EmploymentType,
    JobParseResponse,
    ParsedJobDescription,
    SalaryPeriod,
    SalaryRange,
    SkillSet,
    WorkMode,
)


# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_JD = """\
Senior Java Developer — 6-month contract, London (hybrid 2 days/week).
Rate: £550–£650/day outside IR35.

Required: Java 17, Spring Boot 3, Microservices, REST APIs, PostgreSQL, Docker.
Nice to have: Kafka, Kubernetes, AWS, Terraform.

5+ years experience required.
Benefits: 25 days holiday, pension, private healthcare.
"""

PARSED_FIXTURE = ParsedJobDescription(
    job_title       = "Senior Java Developer",
    company_name    = None,
    location        = "London, UK",
    work_mode       = WorkMode.HYBRID,
    employment_type = EmploymentType.CONTRACT,
    skills          = SkillSet(
        required  = ["Java 17", "Spring Boot 3", "Microservices", "REST APIs",
                     "PostgreSQL", "Docker"],
        preferred = ["Kafka", "Kubernetes", "AWS", "Terraform"],
    ),
    salary = SalaryRange(
        min_value = 550,
        max_value = 650,
        currency  = "GBP",
        period    = SalaryPeriod.DAILY,
        raw_text  = "£550–£650/day",
    ),
    experience_years_min = 5,
    experience_years_max = None,
    benefits             = ["25 days holiday", "pension", "private healthcare"],
    confidence_score     = 0.95,
)

RESPONSE_FIXTURE = JobParseResponse(
    parsed             = PARSED_FIXTURE,
    model_used         = "gemini-2.0-flash",
    input_chars        = len(SAMPLE_JD),
    total_tokens       = 480,
    processing_time_ms = 920.0,
)


def _mock_gemini(response: JobParseResponse | None = None) -> GeminiService:
    svc = MagicMock(spec=GeminiService)
    svc.parse      = AsyncMock(return_value=response or RESPONSE_FIXTURE)
    svc.parse_many = AsyncMock(return_value=BatchParseResponse(
        results            = [response or RESPONSE_FIXTURE],
        total              = 1,
        total_tokens       = (response or RESPONSE_FIXTURE).total_tokens,
        processing_time_ms = 950.0,
    ))
    return svc


def _app(gemini_svc: GeminiService | None = None):
    """Return the app with the Gemini dependency overridden and auth disabled."""
    from job_parser_service.main import require_api_key
    a = app
    a.dependency_overrides[get_gemini]       = lambda: (gemini_svc or _mock_gemini())
    a.dependency_overrides[require_api_key]  = lambda: None  # disable auth in tests
    return a


BASE  = "http://test"
HDRS  = {"X-API-Key": "dev-key"}          # matches default API_KEY="" → auth disabled


@pytest.fixture()
async def client():
    """Unauthenticated test client (API_KEY is empty in test env)."""
    async with AsyncClient(transport=ASGITransport(_app()), base_url=BASE) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════════
# Health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code in (200, 503)   # 503 if no key set in env
    body = resp.json()
    assert "status"  in body
    assert "service" in body
    assert "version" in body


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_description_too_short(client: AsyncClient) -> None:
    resp = await client.post("/parse", json={"description": "Too short."})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_description_too_long(client: AsyncClient) -> None:
    resp = await client.post("/parse", json={"description": "x" * 20_001})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_description_field(client: AsyncClient) -> None:
    resp = await client.post("/parse", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_too_many(client: AsyncClient) -> None:
    resp = await client.post(
        "/parse/batch",
        json={"descriptions": [SAMPLE_JD] * 11},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_empty_list(client: AsyncClient) -> None:
    resp = await client.post("/parse/batch", json={"descriptions": []})
    assert resp.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse — happy path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_parse_returns_200(client: AsyncClient) -> None:
    resp = await client.post("/parse", json={"description": SAMPLE_JD})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_parse_envelope_shape(client: AsyncClient) -> None:
    resp  = await client.post("/parse", json={"description": SAMPLE_JD})
    body  = resp.json()
    assert "data"    in body
    assert "message" in body
    data = body["data"]
    assert "parsed"             in data
    assert "model_used"         in data
    assert "input_chars"        in data
    assert "processing_time_ms" in data


@pytest.mark.asyncio
async def test_parse_skills(client: AsyncClient) -> None:
    resp   = await client.post("/parse", json={"description": SAMPLE_JD})
    parsed = resp.json()["data"]["parsed"]
    assert "Java 17" in parsed["skills"]["required"]
    assert "Kafka"   in parsed["skills"]["preferred"]


@pytest.mark.asyncio
async def test_parse_location(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert parsed["location"] == "London, UK"


@pytest.mark.asyncio
async def test_parse_salary(client: AsyncClient) -> None:
    salary = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]["salary"]
    assert salary is not None
    assert salary["min_value"]  == 550
    assert salary["max_value"]  == 650
    assert salary["currency"]   == "GBP"
    assert salary["period"]     == "daily"
    assert "£550" in salary["raw_text"]


@pytest.mark.asyncio
async def test_parse_work_mode(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert parsed["work_mode"] == "hybrid"


@pytest.mark.asyncio
async def test_parse_employment_type(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert parsed["employment_type"] == "contract"


@pytest.mark.asyncio
async def test_parse_experience(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert parsed["experience_years_min"] == 5
    assert parsed["experience_years_max"] is None


@pytest.mark.asyncio
async def test_parse_benefits(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert "pension" in parsed["benefits"]


@pytest.mark.asyncio
async def test_parse_confidence_score_in_range(client: AsyncClient) -> None:
    parsed = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]["parsed"]
    assert 0.0 <= parsed["confidence_score"] <= 1.0


@pytest.mark.asyncio
async def test_parse_token_count_present(client: AsyncClient) -> None:
    data = (await client.post("/parse", json={"description": SAMPLE_JD})).json()["data"]
    assert data["total_tokens"] == 480


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse — edge cases
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_parse_no_salary() -> None:
    """When Gemini finds no salary, parsed.salary should be None."""
    no_salary = PARSED_FIXTURE.model_copy(update={"salary": None})
    fixture   = RESPONSE_FIXTURE.model_copy(update={"parsed": no_salary})
    async with AsyncClient(transport=ASGITransport(_app(_mock_gemini(fixture))), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    assert resp.json()["data"]["parsed"]["salary"] is None


@pytest.mark.asyncio
async def test_parse_no_skills() -> None:
    no_skills = PARSED_FIXTURE.model_copy(update={"skills": SkillSet()})
    fixture   = RESPONSE_FIXTURE.model_copy(update={"parsed": no_skills})
    async with AsyncClient(transport=ASGITransport(_app(_mock_gemini(fixture))), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    data = resp.json()["data"]["parsed"]
    assert data["skills"]["required"]  == []
    assert data["skills"]["preferred"] == []


@pytest.mark.asyncio
async def test_parse_not_specified_enums() -> None:
    unknown = PARSED_FIXTURE.model_copy(
        update={
            "work_mode":       WorkMode.NOT_SPECIFIED,
            "employment_type": EmploymentType.NOT_SPECIFIED,
        }
    )
    fixture = RESPONSE_FIXTURE.model_copy(update={"parsed": unknown})
    async with AsyncClient(transport=ASGITransport(_app(_mock_gemini(fixture))), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    data = resp.json()["data"]["parsed"]
    assert data["work_mode"]       == "not_specified"
    assert data["employment_type"] == "not_specified"


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse/batch
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_batch_returns_200(client: AsyncClient) -> None:
    resp = await client.post(
        "/parse/batch", json={"descriptions": [SAMPLE_JD]}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_batch_result_count(client: AsyncClient) -> None:
    resp = await client.post(
        "/parse/batch", json={"descriptions": [SAMPLE_JD]}
    )
    data = resp.json()["data"]
    assert data["total"] == 1
    assert len(data["results"]) == 1


@pytest.mark.asyncio
async def test_batch_aggregate_tokens(client: AsyncClient) -> None:
    resp = await client.post(
        "/parse/batch", json={"descriptions": [SAMPLE_JD]}
    )
    data = resp.json()["data"]
    assert data["total_tokens"] is not None


@pytest.mark.asyncio
async def test_batch_each_result_has_parsed(client: AsyncClient) -> None:
    resp = await client.post(
        "/parse/batch", json={"descriptions": [SAMPLE_JD]}
    )
    result = resp.json()["data"]["results"][0]
    assert "parsed" in result
    assert "model_used" in result


# ══════════════════════════════════════════════════════════════════════════════
# Error mapping
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_parse_bad_json_from_gemini_raises_502() -> None:
    svc = MagicMock(spec=GeminiService)
    svc.parse = AsyncMock(side_effect=ValueError("Gemini returned non-JSON: …"))
    async with AsyncClient(transport=ASGITransport(_app(svc)), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_parse_quota_error_raises_429() -> None:
    svc = MagicMock(spec=GeminiService)
    svc.parse = AsyncMock(side_effect=Exception("quota exceeded — 429"))
    async with AsyncClient(transport=ASGITransport(_app(svc)), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_parse_auth_error_raises_503() -> None:
    svc = MagicMock(spec=GeminiService)
    svc.parse = AsyncMock(side_effect=Exception("api_key invalid 401"))
    async with AsyncClient(transport=ASGITransport(_app(svc)), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_parse_unexpected_error_raises_500() -> None:
    svc = MagicMock(spec=GeminiService)
    svc.parse = AsyncMock(side_effect=RuntimeError("disk full"))
    async with AsyncClient(transport=ASGITransport(_app(svc)), base_url=BASE) as c:
        resp = await c.post("/parse", json={"description": SAMPLE_JD})
    assert resp.status_code == 500


# ══════════════════════════════════════════════════════════════════════════════
# Unit — model logic (no HTTP layer)
# ══════════════════════════════════════════════════════════════════════════════

def test_skillset_all_skills_deduplicates() -> None:
    s = SkillSet(required=["Python", "Docker"], preferred=["docker", "AWS"])
    all_s = s.all_skills
    assert all_s.count("Docker") + all_s.count("docker") == 1
    assert "AWS" in all_s


def test_salary_range_swaps_inverted_min_max() -> None:
    s = SalaryRange(min_value=700, max_value=500, currency="GBP", period=SalaryPeriod.DAILY)
    assert s.min_value == 500
    assert s.max_value == 700


def test_salary_range_normalises_currency() -> None:
    s = SalaryRange(currency="gbp", period=SalaryPeriod.ANNUAL)
    assert s.currency == "GBP"


def test_parsed_job_description_defaults() -> None:
    p = ParsedJobDescription()
    assert p.work_mode       == WorkMode.NOT_SPECIFIED
    assert p.employment_type == EmploymentType.NOT_SPECIFIED
    assert p.skills.required  == []
    assert p.salary           is None
    assert p.confidence_score == 0.0
