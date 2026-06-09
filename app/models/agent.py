"""Agent configuration and runtime state models."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentType(StrEnum):
    HARVEST = "harvest"
    SCRAPER = "scraper"
    ORCHESTRATOR = "orchestrator"


class AgentStatus(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REFLECTING = "reflecting"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentConfig(BaseModel):
    """Configuration passed when creating or running an agent."""

    agent_type: AgentType = AgentType.HARVEST
    model: str = "claude-sonnet-4-6"
    max_iterations: int = Field(20, ge=1, le=100)
    temperature: float = Field(0.0, ge=0.0, le=1.0)
    enable_screenshots: bool = True
    enable_javascript: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentStep(BaseModel):
    """Single step/action taken by an agent."""

    step_number: int
    action: str
    action_input: dict[str, Any]
    observation: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentState(BaseModel):
    """Live runtime state of a running agent."""

    agent_id: str
    job_id: str
    agent_type: AgentType
    status: AgentStatus = AgentStatus.IDLE
    current_url: str = ""
    pages_visited: list[str] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    iteration: int = 0
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    error: str | None = None


class AgentRunRequest(BaseModel):
    """Request body for POST /agents/run."""

    url: str
    goal: str
    config: AgentConfig = Field(default_factory=AgentConfig)
    job_id: str | None = None  # Attach to existing harvest job


class AgentRunResponse(BaseModel):
    agent_id: str
    job_id: str
    status: AgentStatus
    message: str
