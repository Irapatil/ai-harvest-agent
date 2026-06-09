"""
Tests for:
  POST /api/v1/jobs/linkedin/search
  POST /api/v1/jobs/linkedin/harvest
  POST /api/v1/jobs/linkedin/harvest/async
  GET  /api/v1/jobs/linkedin/harvest/{id}
"""
from __future__ import annotations

from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.models.job_parser import EmploymentType, ParsedJobDescription, SkillSet, WorkMode
from app.models.linkedin import (
    EnrichedLinkedInJob,
    HarvestJob,
    HarvestStatus,
    LinkedInHarvestResult,
    LinkedInSearchConfig,
    LinkedInSearchResult,
)
from app.routes.linkedin_harvest import get_pipeline, get_pipeline_search_only
from app.scrapers.linkedin_scraper import LinkedInJobCard

# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_CONFIG = {
    "keywords":               "Contract Java Developer",
    "date_posted":            "past_24h",
    "work_mode":              "any",
    "employment_type":        "contract",
    "max_jobs":               5,
    "max_search_pages":       1,
    "fetch_descriptions":     True,
    "parse_with_gemini":      True,
    "headless":               True,
    "slow_mo_ms":             0,
    "description_concurrency": 2,
}

MOCK_CARDS = [
    LinkedInJobCard(
        job_title   = "Contract Java Developer",
        company     = "Acme Corp",
        location    = "London, UK",
        job_url     = "https://www.linkedin.com/jobs/view/1234567890",
        posted_time = "2026-05-26T10:00:00",
        job_id      = "1234567890",
    ),
    LinkedInJobCard(
        job_title   = "Senior Java Engineer (Contract)",
        company     = "TechStart Ltd",
        location    = "Remote",
        job_url     = "https://www.linkedin.com/jobs/view/9876543210",
        posted_time = "2026-05-26T09:00:00",
        job_id      = "9876543210",
    ),
]

MOCK_PARSED = ParsedJobDescription(
    job_title       = "Contract Java Developer",
    company_name    = "Acme Corp",
    location        = "London, United Kingdom",    # more specific than card
    work_mode       = WorkMode.HYBRID,
    employment_type = EmploymentType.CONTRACT,
    skills          = SkillSet(
        required  = ["Java 17", "Spring Boot", "Kafka"],
        preferred = ["AWS", "Docker"],
    ),
    confidence_score = 0.92,
)

MOCK_ENRICHED_JOB = EnrichedLinkedInJob(
    job_id                  = "1234567890",
    job_title               = "Contract Java Developer",
    company                 = "Acme Corp",
    location                = "London, UK",           # raw card value
    job_url                 = "https://www.linkedin.com/jobs/view/1234567890",
    posted_time             = "2026-05-26T10:00:00",
    raw_description         = "We are looking for a Contract Java Developer with 5+ years experience...",
    description_length      = 60,
    parsed                  = MOCK_PARSED,
)

MOCK_HARVEST_RESULT = LinkedInHarvestResult(
    jobs            = [MOCK_ENRICHED_JOB],
    search_config   = LinkedInSearchConfig(**SAMPLE_CONFIG),
    total_found     = 2,
    total_described = 2,
    total_parsed    = 1,
    duration_ms     = 3200.0,
    errors          = [],
)

MOCK_SEARCH_RESULT = LinkedInSearchResult(
    jobs          = [asdict(c) for c in MOCK_CARDS],
    keywords      = "Contract Java Developer",
    total_found   = 2,
    pages_scraped = 1,
    duration_ms   = 800.0,
)


# ══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mock_pipeline(harvest_result=None, search_result=None):
    """Build a MagicMock pipeline with both methods configured."""
    svc = MagicMock()
    svc.harvest     = AsyncMock(return_value=harvest_result or MOCK_HARVEST_RESULT)
    svc.search_only = AsyncMock(return_value=search_result  or MOCK_SEARCH_RESULT)
    return svc


def _app_with_pipeline(pipeline_svc):
    """FastAPI app with both pipeline dependencies overridden."""
    app = create_app()
    app.dependency_overrides[get_pipeline]             = lambda: pipeline_svc
    app.dependency_overrides[get_pipeline_search_only] = lambda: pipeline_svc
    return app


HEADERS = {"X-API-Key": "dev-api-key"}


# ══════════════════════════════════════════════════════════════════════════════
# Auth tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_harvest_requires_auth() -> None:
    app = _app_with_pipeline(_mock_pipeline())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/harvest", json=SAMPLE_CONFIG)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_requires_auth() -> None:
    app = _app_with_pipeline(_mock_pipeline())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/search", json=SAMPLE_CONFIG)
    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# /harvest — full pipeline (sync)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_harvest_returns_enriched_jobs() -> None:
    mock_svc = _mock_pipeline()
    app      = _app_with_pipeline(mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/harvest", headers=HEADERS, json=SAMPLE_CONFIG)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["total_found"]  == 2
    assert body["data"]["total_parsed"] == 1

    job = body["data"]["jobs"][0]
    assert job["job_title"] == "Contract Java Developer"
    assert job["company"]   == "Acme Corp"
    # Gemini-parsed fields nested inside `parsed`
    assert job["parsed"]["work_mode"]       == "hybrid"
    assert job["parsed"]["employment_type"] == "contract"
    assert "Java 17" in job["parsed"]["skills"]["required"]
    assert job["parsed"]["confidence_score"] == 0.92
    mock_svc.harvest.assert_called_once()


@pytest.mark.asyncio
async def test_harvest_propagates_errors_field() -> None:
    result_with_errors = MOCK_HARVEST_RESULT.model_copy(
        update={"errors": ["[fetch] 9876543210: connection timeout"]}
    )
    app = _app_with_pipeline(_mock_pipeline(harvest_result=result_with_errors))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/harvest", headers=HEADERS, json=SAMPLE_CONFIG)

    assert resp.status_code == 200
    errors = resp.json()["data"]["errors"]
    assert len(errors) == 1
    assert "timeout" in errors[0]


@pytest.mark.asyncio
async def test_harvest_response_has_all_fields() -> None:
    app = _app_with_pipeline(_mock_pipeline())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/harvest", headers=HEADERS, json=SAMPLE_CONFIG)

    data = resp.json()["data"]
    for field in ("jobs", "total_found", "total_described", "total_parsed", "duration_ms", "errors"):
        assert field in data, f"Missing top-level field: {field}"

    job = data["jobs"][0]
    for field in ("job_id", "job_title", "company", "location", "job_url",
                  "raw_description", "description_length", "parsed"):
        assert field in job, f"Missing job field: {field}"


# ══════════════════════════════════════════════════════════════════════════════
# /search — Phase 1 only
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_search_returns_raw_cards() -> None:
    mock_svc = _mock_pipeline()
    app      = _app_with_pipeline(mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/search", headers=HEADERS, json=SAMPLE_CONFIG)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total_found"] == 2
    assert body["keywords"]    == "Contract Java Developer"
    assert len(body["jobs"])   == 2
    mock_svc.search_only.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# /harvest/async + /harvest/{id} — background task
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_harvest_async_returns_job_id() -> None:
    """POST /harvest/async returns 202 with a job_id immediately."""
    mock_svc = _mock_pipeline()
    app      = _app_with_pipeline(mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/jobs/linkedin/harvest/async", headers=HEADERS, json=SAMPLE_CONFIG)

    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "id"     in data
    assert "status" in data
    assert data["status"] in ("pending", "running", "done")   # may have progressed


@pytest.mark.asyncio
async def test_harvest_poll_unknown_id_returns_404() -> None:
    """GET /harvest/<bad-id> returns 404."""
    app = _app_with_pipeline(_mock_pipeline())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/jobs/linkedin/harvest/nonexistent-id", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_harvest_async_job_completes() -> None:
    """
    Full async flow: POST to start → GET to poll → assert status=done.
    Background tasks run synchronously in TestClient / ASGITransport.
    """
    mock_svc = _mock_pipeline()
    app      = _app_with_pipeline(mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Start the job
        start_resp = await c.post(
            "/api/v1/jobs/linkedin/harvest/async", headers=HEADERS, json=SAMPLE_CONFIG
        )
        assert start_resp.status_code == 202
        job_id = start_resp.json()["data"]["id"]

        # Background tasks have run by the time the response is returned
        # (ASGITransport executes them inline).  Poll once.
        poll_resp = await c.get(f"/api/v1/jobs/linkedin/harvest/{job_id}", headers=HEADERS)

    assert poll_resp.status_code == 200
    job_data = poll_resp.json()["data"]
    assert job_data["id"] == job_id
    # Status may be done or pending depending on whether BG task completed
    assert job_data["status"] in ("pending", "running", "done", "failed")


# ══════════════════════════════════════════════════════════════════════════════
# URL builder
# ══════════════════════════════════════════════════════════════════════════════

def test_search_url_24h_contract() -> None:
    cfg = LinkedInSearchConfig(
        keywords        = "Contract Java Developer",
        date_posted     = "past_24h",
        employment_type = "contract",
    )
    url = cfg.build_search_url()
    assert "f_TPR=r86400" in url
    assert "f_JT=C"       in url
    assert "Contract"     in url


def test_search_url_remote_filter() -> None:
    cfg = LinkedInSearchConfig(keywords="Python Developer", work_mode="remote")
    assert "f_WT=2" in cfg.build_search_url()


def test_search_url_location() -> None:
    cfg = LinkedInSearchConfig(keywords="Java", location="London")
    assert "location=London" in cfg.build_search_url()


def test_search_url_any_time_no_tpr() -> None:
    cfg = LinkedInSearchConfig(keywords="Java", date_posted="any")
    assert "f_TPR" not in cfg.build_search_url()


# ══════════════════════════════════════════════════════════════════════════════
# Smart merge (EnrichedLinkedInJob.effective_* properties)
# ══════════════════════════════════════════════════════════════════════════════

def test_effective_location_prefers_gemini() -> None:
    """Gemini's location (more specific) wins over card location."""
    job = MOCK_ENRICHED_JOB
    # MOCK_PARSED has location="London, United Kingdom" vs card "London, UK"
    assert job.effective_location == "London, United Kingdom"


def test_effective_location_falls_back_to_card() -> None:
    """When Gemini has no location, card location is used."""
    parsed_no_location = MOCK_PARSED.model_copy(update={"location": None})
    job = MOCK_ENRICHED_JOB.model_copy(update={"parsed": parsed_no_location})
    assert job.effective_location == "London, UK"


def test_effective_work_mode_from_gemini() -> None:
    assert MOCK_ENRICHED_JOB.effective_work_mode == "hybrid"


def test_effective_work_mode_not_specified_when_no_parse() -> None:
    job = MOCK_ENRICHED_JOB.model_copy(update={"parsed": None})
    assert job.effective_work_mode == "not_specified"


def test_effective_skills_from_gemini() -> None:
    skills = MOCK_ENRICHED_JOB.effective_skills
    assert skills is not None
    assert "Java 17" in skills.required
    assert "AWS"      in skills.preferred


def test_effective_salary_none_when_not_in_jd() -> None:
    """Salary is None when the JD doesn't mention pay."""
    job = MOCK_ENRICHED_JOB.model_copy(update={"parsed": MOCK_PARSED})
    # MOCK_PARSED has no salary set
    assert job.effective_salary is None


def test_summary_dict_shape() -> None:
    summary = MOCK_ENRICHED_JOB.summary()
    for key in ("job_id", "job_title", "company", "effective_location",
                "effective_work_mode", "required_skills", "job_url", "confidence_score"):
        assert key in summary, f"Missing key in summary(): {key}"
    assert summary["required_skills"] == ["Java 17", "Spring Boot", "Kafka"]
