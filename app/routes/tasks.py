"""Celery background task management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.security import require_api_key
from app.models.response import APIResponse
from app.models.task import Task, TaskStatus
from app.services.queue_service import get_task_status, revoke_task

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get(
    "/{task_id}",
    response_model=APIResponse[Task],
    summary="Get Celery task status",
)
async def get_task(task_id: str) -> APIResponse[Task]:
    info = get_task_status(task_id)
    task = Task(
        task_id=task_id,
        task_name="harvest.run_job",
        status=TaskStatus(info["status"]),
        result=info.get("result"),
        error=info.get("error"),
    )
    return APIResponse(data=task)


@router.post(
    "/{task_id}/cancel",
    response_model=APIResponse[dict],
    summary="Cancel a running task",
)
async def cancel_task(task_id: str) -> APIResponse[dict]:
    revoke_task(task_id, terminate=True)
    return APIResponse(data={"task_id": task_id, "cancelled": True})
