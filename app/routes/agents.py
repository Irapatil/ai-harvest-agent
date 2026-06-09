"""Agent introspection and direct-run endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.agents.orchestrator import AgentOrchestrator
from app.core.dependencies import get_llm_service, get_playwright_service
from app.core.security import require_api_key
from app.models.agent import AgentRunRequest, AgentRunResponse, AgentStatus
from app.models.response import APIResponse
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

import uuid

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get(
    "",
    response_model=APIResponse[list[dict]],
    summary="List available agent types",
)
async def list_agents(
    llm: LLMService = Depends(get_llm_service),
    playwright: PlaywrightService = Depends(get_playwright_service),
) -> APIResponse[list[dict]]:
    orchestrator = AgentOrchestrator(llm, playwright)
    return APIResponse(data=orchestrator.list_agents())


@router.post(
    "/run",
    response_model=APIResponse[AgentRunResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an agent directly (synchronous, use for small tasks)",
)
async def run_agent(
    payload: AgentRunRequest,
    llm: LLMService = Depends(get_llm_service),
    playwright: PlaywrightService = Depends(get_playwright_service),
) -> APIResponse[AgentRunResponse]:
    job_id = payload.job_id or str(uuid.uuid4())
    orchestrator = AgentOrchestrator(llm, playwright)
    await orchestrator.run(
        job_id=job_id,
        url=payload.url,
        goal=payload.goal,
        config=payload.config,
    )
    return APIResponse(
        data=AgentRunResponse(
            agent_id=str(uuid.uuid4()),
            job_id=job_id,
            status=AgentStatus.COMPLETED,
            message="Agent run completed",
        )
    )
