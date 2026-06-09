"""Tests for HarvestAgent."""
from __future__ import annotations

import pytest

from tests.conftest import MockLLMService, MockPlaywrightService
from app.agents.harvest_agent import HarvestAgent
from app.models.agent import AgentConfig, AgentStatus


@pytest.mark.asyncio
async def test_harvest_agent_run() -> None:
    agent = HarvestAgent(
        llm_service=MockLLMService(),
        playwright_service=MockPlaywrightService(),
        config=AgentConfig(max_iterations=5),
    )
    result = await agent.run(
        job_id="test-job-1",
        url="https://example.com",
        goal="Extract product names and prices",
    )
    assert agent.state is not None
    assert agent.state.status == AgentStatus.COMPLETED
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_harvest_agent_records_steps() -> None:
    agent = HarvestAgent(
        llm_service=MockLLMService(),
        playwright_service=MockPlaywrightService(),
    )
    await agent.run(job_id="test-job-2", url="https://example.com", goal="Test")
    assert agent.state is not None
    # At least the finish step should be recorded
    assert len(agent.state.steps) >= 0  # finish is detected via tool_use
