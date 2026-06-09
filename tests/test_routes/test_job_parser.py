"""Tests for POST /api/v1/jobs/parse."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.models.job_parser import (
    EmploymentType,
    JobParseResponse,
    ParsedJobDescription,
    SalaryPeriod,
    SalaryRange,
    SkillSet,
    WorkMode,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_JD = """
We are looking for a Senior Java Developer on a 6-month contract basis.
The role is fully remote. You will work on microservices using Java 17,
Spring Boot, Kafka, Docker, and Kubernetes.

Required skills: Java, Spring Boot, Kafka, Docker, Kubernetes, REST APIs.
Nice to have: AWS, Terraform, Python.

Salary: £500–£650/day. 5+ years experience required.
Benefits include 25 days holiday, pension, and private healthcare.
"""

MOCK_PARSED = ParsedJobDescription(
    job_title="Senior Java Developer",
    company_name=None,
    location="Remote",
    work_mode=WorkMode.REMOTE,
    employment_type=EmploymentType.CONTRACT,
    skills=SkillSet(
        required=["Java", "Spring Boot", "Kafka", "Docker", "Kubernetes", "REST APIs"],
        preferred=["AWS", "Terraform", "Python"],
    ),
    salary=SalaryRange(
        min_value=500,
        max_value=650,
        currency="GBP",
        period=SalaryPeriod.DAILY,
        raw_text="£500–£650/day",
    ),
    experience_years_min=5,
    experience_years_max=None,
    education_requirement=None,
    benefits=["25 days holiday", "pension", "private healthcare"],
    languages=[],
    confidence_score=0.95,
    extraction_notes=None,
)

MOCK_RESPONSE = JobParseResponse(
    parsed=MOCK_PARSED,
    model_used="gemini-2.0-flash",
    input_chars=len(SAMPLE_JD),
    total_tokens=312,
    processing_time_ms=780.0,
)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/jobs/parse",
        json={"description": SAMPLE_JD},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_parse_too_short() -> None:
    """Pydantic rejects descriptions shorter than 50 chars with 422."""
    from httpx import ASGITransport, AsyncClient as _AC
    from app.routes.job_parser import get_gemini as get_gemini_service
    from app.main import create_app

    mock_svc = MagicMock()
    mock_svc.parse_job_description = AsyncMock(return_value=MOCK_RESPONSE)

    app = create_app()
    app.dependency_overrides[get_gemini_service] = lambda: mock_svc

    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/jobs/parse",
            headers={"X-API-Key": "dev-api-key"},
            json={"description": "Too short"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_parse_job_description(client: AsyncClient) -> None:
    """Happy-path: mock GeminiService and verify response shape."""
    mock_svc = MagicMock()
    mock_svc.parse_job_description = AsyncMock(return_value=MOCK_RESPONSE)

    from app.routes.job_parser import get_gemini as get_gemini_service
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_gemini_service] = lambda: mock_svc

    from httpx import ASGITransport, AsyncClient as _AC

    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/jobs/parse",
            headers={"X-API-Key": "dev-api-key"},
            json={"description": SAMPLE_JD},
        )

    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]["parsed"]

    assert data["work_mode"] == "remote"
    assert data["employment_type"] == "contract"
    assert "Java" in data["skills"]["required"]
    assert data["salary"]["currency"] == "GBP"
    assert data["salary"]["period"] == "daily"
    assert data["confidence_score"] == 0.95


@pytest.mark.asyncio
async def test_parse_returns_all_fields(client: AsyncClient) -> None:
    """Verify every top-level field is present in the response."""
    mock_svc = MagicMock()
    mock_svc.parse_job_description = AsyncMock(return_value=MOCK_RESPONSE)

    from app.routes.job_parser import get_gemini as get_gemini_service
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_gemini_service] = lambda: mock_svc

    from httpx import ASGITransport, AsyncClient as _AC

    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/jobs/parse",
            headers={"X-API-Key": "dev-api-key"},
            json={"description": SAMPLE_JD},
        )

    body = resp.json()["data"]
    assert "parsed" in body
    assert "model_used" in body
    assert "processing_time_ms" in body
    parsed = body["parsed"]
    for field in ("job_title", "location", "work_mode", "employment_type",
                  "skills", "salary", "benefits", "confidence_score"):
        assert field in parsed, f"Missing field: {field}"
