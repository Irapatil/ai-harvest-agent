"""AgentOrchestrator: selects and runs the right agent for a job."""
from __future__ import annotations

from typing import Any

import structlog

from app.agents.harvest_agent import HarvestAgent
from app.agents.scraper_agent import ScraperAgent
from app.models.agent import AgentConfig, AgentType
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

logger = structlog.get_logger(__name__)

_AGENT_REGISTRY = {
    AgentType.HARVEST: HarvestAgent,
    AgentType.SCRAPER: ScraperAgent,
}


class AgentOrchestrator:
    """
    Routes job requests to the appropriate agent implementation.
    Supports future multi-agent workflows (e.g., discovery + extraction pipeline).
    """

    def __init__(
        self,
        llm_service: LLMService,
        playwright_service: PlaywrightService,
    ) -> None:
        self._llm = llm_service
        self._playwright = playwright_service

    async def run(
        self,
        job_id: str,
        url: str,
        goal: str,
        config: AgentConfig | None = None,
    ) -> dict[str, Any]:
        """Instantiate the correct agent and run it."""
        cfg = config or AgentConfig()
        agent_cls = _AGENT_REGISTRY.get(cfg.agent_type)
        if agent_cls is None:
            raise ValueError(f"Unknown agent type: {cfg.agent_type}")

        logger.info(
            "orchestrator_dispatching",
            agent_type=cfg.agent_type,
            job_id=job_id,
            url=url,
        )
        agent = agent_cls(
            llm_service=self._llm,
            playwright_service=self._playwright,
            config=cfg,
        )
        result = await agent.run(job_id=job_id, url=url, goal=goal)
        logger.info("orchestrator_done", job_id=job_id, agent_type=cfg.agent_type)
        return result

    async def run_pipeline(
        self,
        job_id: str,
        url: str,
        goal: str,
        config: AgentConfig | None = None,
    ) -> dict[str, Any]:
        """
        Two-stage pipeline: HarvestAgent discovers URLs, ScraperAgent extracts.
        Useful for catalogue pages → detail pages.
        """
        cfg = config or AgentConfig()

        # Stage 1: Discovery
        discovery_cfg = AgentConfig(**{**cfg.model_dump(), "agent_type": AgentType.HARVEST})
        harvester = HarvestAgent(self._llm, self._playwright, discovery_cfg)
        discovery = await harvester.run(
            job_id=job_id,
            url=url,
            goal=f"Find all individual item URLs related to: {goal}",
        )

        detail_urls: list[str] = discovery.get("_pages_visited", [])
        if not detail_urls:
            logger.warning("no_detail_urls_found", job_id=job_id)
            return discovery

        # Stage 2: Extraction (run scraper on each URL)
        all_records: list[dict[str, Any]] = []
        scraper_cfg = AgentConfig(**{**cfg.model_dump(), "agent_type": AgentType.SCRAPER})
        for detail_url in detail_urls[:cfg.max_iterations]:
            scraper = ScraperAgent(self._llm, self._playwright, scraper_cfg)
            result = await scraper.run(job_id=job_id, url=detail_url, goal=goal)
            all_records.extend(result.get("records", []))

        return {"records": all_records, "total": len(all_records), "pipeline": "two-stage"}

    def list_agents(self) -> list[dict[str, str]]:
        return [
            {"type": k.value, "description": cls.__doc__ or ""}
            for k, cls in _AGENT_REGISTRY.items()
        ]
