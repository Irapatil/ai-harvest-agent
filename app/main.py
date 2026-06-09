"""FastAPI application factory and lifespan."""
from __future__ import annotations

import asyncio
import sys
import structlog
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.exceptions import HarvestException, harvest_exception_handler
from app.core.middleware import LoggingMiddleware, RateLimitMiddleware
from app.routes import harvest, agents, tasks, health, job_parser, linkedin_harvest
from app.routes.harvest_routes import router as harvest_agent_router
from app.routes.linkedin_routes import router as linkedin_agent_router
from app.routes.naukri_routes import router as naukri_agent_router
from app.routes.dice_routes import router as dice_agent_router
from app.routes.run_harvest_agent import router as run_harvest_agent_router
from app.services.playwright_service import PlaywrightService
from app.services.scheduler_service import SchedulerService

logger   = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: launch browser pool + scheduler. Shutdown: clean up both."""
    logger.info("startup", env=settings.app_env, model=settings.anthropic_model)

    # ── Playwright pool (optional — demo routes create their own browser) ─────
    # On Windows with --reload, uvicorn forces SelectorEventLoop which cannot
    # spawn Playwright's browser subprocess.  The pool is skipped gracefully;
    # scraper routes use run_in_proactor() to launch browsers in a worker thread.
    #
    # We temporarily install a custom asyncio exception handler to suppress the
    # "Task exception was never retrieved" noise that Playwright's internal
    # Connection.run() background Task emits when the subprocess fails on
    # SelectorEventLoop.  The handler is restored to default afterward.
    loop = asyncio.get_event_loop()
    if sys.platform == "win32":
        def _suppress_not_impl(loop, context):
            if isinstance(context.get("exception"), NotImplementedError):
                return
            loop.default_exception_handler(context)
        loop.set_exception_handler(_suppress_not_impl)

    playwright_service = PlaywrightService(settings)
    try:
        await playwright_service.start()
        app.state.playwright = playwright_service
        logger.info("playwright_ready", pool_size=settings.playwright_pool_size)
    except NotImplementedError:
        logger.info(
            "playwright_pool_skipped",
            reason="SelectorEventLoop (uvicorn --reload on Windows) — scraper routes use ProactorEventLoop thread",
        )
        app.state.playwright = None
    except Exception as exc:
        logger.warning("playwright_pool_unavailable", error=str(exc))
        app.state.playwright = None
    finally:
        if sys.platform == "win32":
            loop.set_exception_handler(None)   # restore default handler

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = SchedulerService()
    scheduler.start()
    app.state.scheduler = scheduler

    # Apply schedule from config (if enabled)
    try:
        from app.services.config_service import ConfigService
        from app.routes.harvest_routes import _apply_schedule
        cfg = ConfigService().load()
        if cfg.schedule.enabled:
            await _apply_schedule(scheduler, cfg)
            logger.info("scheduler_schedule_applied", frequency=cfg.schedule.frequency)
    except Exception as exc:
        logger.warning("scheduler_setup_failed", error=str(exc))

    yield  # ← app runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    try:
        await playwright_service.stop()
    except Exception:
        pass
    scheduler.stop()
    logger.info("shutdown_complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title        = "LinkedIn Harvest Agent",
        version      = "1.0.0",
        description  = "",
        openapi_tags = [],
        docs_url     = "/docs",
        redoc_url    = None,
        openapi_url  = "/openapi.json",
        swagger_ui_parameters = {
            "defaultModelsExpandDepth": -1,
            "defaultModelExpandDepth":  -1,
            "defaultModelRendering":    "example",
            "docExpansion":             "full",
            "tryItOutEnabled":          True,
            "displayRequestDuration":   True,
            "filter":                   False,
            "showExtensions":           False,
            "showCommonExtensions":     False,
            "syntaxHighlight.theme":    "monokai",
        },
        lifespan = lifespan,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins    = settings.cors_origins,
        allow_credentials = True,
        allow_methods    = ["*"],
        allow_headers    = ["*"],
    )
    app.add_middleware(LoggingMiddleware)
    if settings.is_production:
        app.add_middleware(RateLimitMiddleware, requests_per_minute=60)

    # ── Exception Handlers ────────────────────────────────────────────────────
    app.add_exception_handler(HarvestException, harvest_exception_handler)  # type: ignore[arg-type]

    # ── Internal routers — hidden from Swagger ────────────────────────────────
    prefix = settings.api_v1_prefix
    app.include_router(health.router,            tags=["Health"],           include_in_schema=False)
    app.include_router(harvest.router,           prefix=f"{prefix}/harvest",        tags=["Harvest"],          include_in_schema=False)
    app.include_router(agents.router,            prefix=f"{prefix}/agents",         tags=["Agents"],           include_in_schema=False)
    app.include_router(tasks.router,             prefix=f"{prefix}/tasks",          tags=["Tasks"],            include_in_schema=False)
    app.include_router(job_parser.router,        prefix=f"{prefix}/jobs",           tags=["Job Parser"],       include_in_schema=False)
    app.include_router(linkedin_harvest.router,  prefix=f"{prefix}/jobs/linkedin",  tags=["LinkedIn Harvest"], include_in_schema=False)

    # ── Public endpoints (Swagger-visible) ────────────────────────────────────
    app.include_router(run_harvest_agent_router)  # POST /run-harvest-agent  ← unified trigger
    app.include_router(linkedin_agent_router)     # POST /run-linkedin-agent  +  results endpoints
    app.include_router(harvest_agent_router)      # POST /run-harvest  +  management endpoints
    app.include_router(naukri_agent_router)       # POST /run-naukri-agent  +  results endpoints
    app.include_router(dice_agent_router)         # POST /run-dice-agent  +  dice results endpoints

    return app


app = create_app()
