"""Abstract base class for all harvest agents."""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import structlog

from app.models.agent import AgentConfig, AgentState, AgentStatus, AgentStep, AgentType
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

logger = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """
    Template for all Harvest Agent implementations.

    Lifecycle:
        plan() → execute() → reflect() → (loop or finish)
    """

    agent_type: AgentType = AgentType.HARVEST

    def __init__(
        self,
        llm_service: LLMService,
        playwright_service: PlaywrightService,
        config: AgentConfig | None = None,
    ) -> None:
        self._llm = llm_service
        self._playwright = playwright_service
        self._config = config or AgentConfig()
        self._state: AgentState | None = None

    # ── Public API ───────────────────────────────────────────────────────────────

    async def run(self, job_id: str, url: str, goal: str) -> dict[str, Any]:
        """Entry point: orchestrate plan → execute → reflect loop."""
        self._state = AgentState(
            agent_id=str(uuid.uuid4()),
            job_id=job_id,
            agent_type=self.agent_type,
            status=AgentStatus.PLANNING,
            started_at=datetime.utcnow(),
        )
        log = logger.bind(agent_id=self._state.agent_id, job_id=job_id)
        log.info("agent_started", url=url)

        try:
            plan = await self.plan(url, goal)
            log.info("plan_ready", plan_summary=str(plan)[:200])

            self._state.status = AgentStatus.EXECUTING
            result = await self.execute(url, goal, plan)

            self._state.status = AgentStatus.REFLECTING
            final = await self.reflect(result, goal)

            self._state.status = AgentStatus.COMPLETED
            self._state.finished_at = datetime.utcnow()
            log.info("agent_completed", pages=len(self._state.pages_visited))
            return final
        except Exception as exc:
            self._state.status = AgentStatus.FAILED
            self._state.error = str(exc)
            self._state.finished_at = datetime.utcnow()
            log.exception("agent_failed", error=str(exc))
            raise

    @property
    def state(self) -> AgentState | None:
        return self._state

    # ── Abstract Methods ─────────────────────────────────────────────────────────

    @abstractmethod
    async def plan(self, url: str, goal: str) -> dict[str, Any]:
        """Produce an execution plan given the start URL and goal."""

    @abstractmethod
    async def execute(
        self, url: str, goal: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute the plan, visiting pages and collecting data."""

    @abstractmethod
    async def reflect(
        self, raw_results: dict[str, Any], goal: str
    ) -> dict[str, Any]:
        """Review and clean the collected data, return final structured output."""

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _record_step(
        self,
        action: str,
        action_input: dict[str, Any],
        observation: str = "",
    ) -> None:
        if self._state is None:
            return
        step = AgentStep(
            step_number=len(self._state.steps) + 1,
            action=action,
            action_input=action_input,
            observation=observation,
        )
        self._state.steps.append(step)
        self._state.iteration += 1

    def _visited(self, url: str) -> None:
        if self._state:
            self._state.current_url = url
            if url not in self._state.pages_visited:
                self._state.pages_visited.append(url)
