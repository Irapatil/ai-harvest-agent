"""FastAPI dependency injection helpers."""
from __future__ import annotations

from typing import AsyncGenerator

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

logger = structlog.get_logger(__name__)

# ── Database ─────────────────────────────────────────────────────────────────────


def _build_engine(settings: Settings):  # type: ignore[return]
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        echo=settings.db_echo,
    )


_engine = None
_session_factory = None


def get_engine(settings: Settings = Depends(get_settings)):
    global _engine
    if _engine is None:
        _engine = _build_engine(settings)
    return _engine


def get_session_factory(settings: Settings = Depends(get_settings)):
    global _session_factory
    if _session_factory is None:
        engine = _build_engine(settings)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


async def get_db_session(
    session_factory=Depends(get_session_factory),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session, rolling back on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── LLM Service ──────────────────────────────────────────────────────────────────


def get_llm_service(settings: Settings = Depends(get_settings)) -> LLMService:
    return LLMService(settings)


# ── Playwright ───────────────────────────────────────────────────────────────────


def get_playwright_service(request: Request) -> PlaywrightService:
    """Return the shared Playwright service from app state."""
    service: PlaywrightService = request.app.state.playwright
    return service
