"""Tests for PlaywrightService (uses mock, not real browser)."""
from __future__ import annotations

import pytest
from tests.conftest import MockPlaywrightService


@pytest.mark.asyncio
async def test_mock_navigate() -> None:
    svc = MockPlaywrightService()
    snap = await svc.navigate("https://example.com")
    assert snap.url == "https://example.com"
    assert snap.title == "Mock Page"
    assert "Test" in snap.text
    assert len(snap.links) > 0
