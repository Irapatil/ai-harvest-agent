"""Harvest job CRUD and async execution endpoints."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db_session, get_llm_service, get_playwright_service
from app.core.security import require_api_key
from app.models.agent import AgentConfig, AgentType
from app.models.harvest import HarvestJob, HarvestJobCreate, HarvestJobWithResults, JobStatus
from app.models.response import APIResponse
from app.services.harvest_service import HarvestService
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService
from app.agents.orchestrator import AgentOrchestrator

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/start",
    response_model=APIResponse[HarvestJob],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new harvest job",
)
async def start_harvest(
    payload: HarvestJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
    llm: LLMService = Depends(get_llm_service),
    playwright: PlaywrightService = Depends(get_playwright_service),
) -> APIResponse[HarvestJob]:
    svc = HarvestService(db)
    job = await svc.create_job(payload)

    async def _run_job() -> None:
        try:
            await svc.mark_running(job.id)
            orchestrator = AgentOrchestrator(llm, playwright)
            result = await orchestrator.run(
                job_id=job.id,
                url=payload.url,
                goal=payload.goal,
                config=AgentConfig(
                    agent_type=AgentType(payload.agent_type),
                    max_iterations=payload.max_pages * 3,
                ),
            )
            await svc.add_result(
                job_id=job.id,
                page_url=payload.url,
                page_number=1,
                extracted_data=result,
            )
            await svc.mark_completed(job.id)
        except Exception as exc:
            await svc.mark_failed(job.id, str(exc))

    background_tasks.add_task(_run_job)
    return APIResponse(data=job, message="Harvest job queued")


@router.get(
    "/{job_id}",
    response_model=APIResponse[HarvestJob],
    summary="Get harvest job status",
)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[HarvestJob]:
    svc = HarvestService(db)
    job = await svc.get_job(job_id)
    return APIResponse(data=job)


@router.get(
    "/{job_id}/results",
    response_model=APIResponse[HarvestJobWithResults],
    summary="Get full job results including extracted data",
)
async def get_job_results(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[HarvestJobWithResults]:
    svc = HarvestService(db)
    job = await svc.get_job_with_results(job_id)
    return APIResponse(data=job)


@router.get(
    "",
    response_model=APIResponse[list[HarvestJob]],
    summary="List all harvest jobs",
)
async def list_jobs(
    status: JobStatus | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[list[HarvestJob]]:
    svc = HarvestService(db)
    jobs = await svc.list_jobs(status=status, page=page, page_size=page_size)
    return APIResponse(data=jobs)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel or delete a harvest job",
    response_class=Response,
)
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    svc = HarvestService(db)
    await svc.delete_job(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
