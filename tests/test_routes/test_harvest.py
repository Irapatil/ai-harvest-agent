"""Tests for /api/v1/harvest endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_start_harvest_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/harvest/start",
        json={"url": "https://example.com", "goal": "test"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_start_harvest(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/harvest/start",
        headers={"X-API-Key": "dev-api-key"},
        json={
            "url": "https://example.com",
            "goal": "Extract all headings",
            "max_pages": 2,
        },
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["url"] == "https://example.com"
    assert data["status"] == "pending"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_job_not_found(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/harvest/nonexistent-id",
        headers={"X-API-Key": "dev-api-key"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_list_jobs_empty(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/harvest",
        headers={"X-API-Key": "dev-api-key"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json()["data"], list)
