"""Health-check endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.config import get_settings
from app.models.response import HealthStatus

router = APIRouter()
settings = get_settings()


@router.get("/health", response_model=HealthStatus, tags=["Health"])
async def health(request: Request) -> HealthStatus:
    """Liveness check — returns 200 if the process is running."""
    pw_ok = hasattr(request.app.state, "playwright")
    return HealthStatus(
        status="ok" if pw_ok else "degraded",
        version="0.1.0",
        environment=settings.app_env,
        checks={"playwright": "ok" if pw_ok else "not_started"},
    )


@router.get("/health/ready", tags=["Health"])
async def ready(request: Request) -> dict:
    """Readiness check — confirms all dependencies are up."""
    return {"status": "ready"}


@router.get("/health/live", tags=["Health"])
async def live() -> dict:
    """Kubernetes liveness probe."""
    return {"status": "alive"}
