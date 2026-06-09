"""Celery app and async task definitions."""
from __future__ import annotations

from app.config import get_settings

settings = get_settings()

# Celery is an optional runtime dependency — not required for tests or the
# job-parser / LinkedIn-harvest routes that use FastAPI BackgroundTasks.
try:
    from celery import Celery
    from celery.result import AsyncResult

    celery_app = Celery(
        "harvest_worker",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.services.queue_service"],
    )

    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
    )
    _CELERY_AVAILABLE = True
except ModuleNotFoundError:
    _CELERY_AVAILABLE = False
    celery_app = None  # type: ignore[assignment]


if _CELERY_AVAILABLE:
    @celery_app.task(bind=True, name="harvest.run_job", max_retries=3)  # type: ignore[misc]
    def run_harvest_job(self, job_id: str, url: str, goal: str, agent_type: str, max_pages: int) -> dict:  # type: ignore[type-arg]
        """
        Background Celery task that runs the harvest agent.
        Uses asyncio.run to bridge sync Celery → async agent code.
        """
        import asyncio

        from app.agents.orchestrator import AgentOrchestrator
        from app.config import get_settings
        from app.models.agent import AgentConfig, AgentType

        settings = get_settings()

        async def _run() -> dict:  # type: ignore[type-arg]
            from app.services.playwright_service import PlaywrightService
            from app.services.llm_service import LLMService

            pw = PlaywrightService(settings)
            await pw.start()
            llm = LLMService(settings)
            orchestrator = AgentOrchestrator(llm_service=llm, playwright_service=pw)
            try:
                result = await orchestrator.run(
                    job_id=job_id,
                    url=url,
                    goal=goal,
                    config=AgentConfig(
                        agent_type=AgentType(agent_type),
                        max_iterations=max_pages * 3,
                    ),
                )
                return result
            finally:
                await pw.stop()

        return asyncio.run(_run())


def get_task_status(task_id: str) -> dict:  # type: ignore[type-arg]
    """Return status dict for a given Celery task ID."""
    if not _CELERY_AVAILABLE or celery_app is None:
        return {"task_id": task_id, "status": "UNKNOWN", "result": None, "error": "Celery not installed"}
    result = celery_app.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "status": result.status,
        "result": result.result if result.ready() else None,
        "error": str(result.result) if result.failed() else None,
    }


def revoke_task(task_id: str, terminate: bool = True) -> None:
    """Cancel a running Celery task."""
    if _CELERY_AVAILABLE and celery_app is not None:
        celery_app.control.revoke(task_id, terminate=terminate)
