"""Shared pytest fixtures.

Imports are kept inside fixture bodies so that test files that don't need
the database / legacy agents can be collected without installing Celery,
SQLAlchemy, or Playwright browser binaries.
"""
from __future__ import annotations

import pytest
import pytest_asyncio


# ── In-memory SQLite for tests ────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.models.harvest import Base

    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


# ── Mock services ─────────────────────────────────────────────────────────────

class MockPlaywrightService:
    async def navigate(self, url, **kwargs):
        from app.services.playwright_service import PageSnapshot
        return PageSnapshot(
            url=url,
            title="Mock Page",
            html="<html><body><h1>Test</h1></body></html>",
            text="Test",
            links=[{"text": "Link", "href": "https://example.com/page2"}],
            forms=[],
        )

    async def start(self): pass
    async def stop(self): pass


class MockLLMService:
    async def complete(self, messages, system="", tools=None):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.stop_reason = "tool_use"
        resp.usage.input_tokens = 100
        resp.usage.output_tokens = 50
        block = MagicMock()
        block.type = "tool_use"
        block.id = "tool_123"
        block.name = "finish"
        block.input = {"data": {"items": []}, "summary": "Done"}
        resp.content = [block]
        return resp

    async def complete_text(self, prompt, system=""):
        return "Mock strategy: navigate, extract, finish."

    async def extract_json(self, content, schema_description, system=""):
        return {"mock_field": "mock_value"}

    def get_tool_use(self, response):
        for block in response.content:
            if block.type == "tool_use":
                return block.id, block.name, block.input
        return None

    def get_text(self, response):
        return ""


# ── FastAPI test client ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session):
    from httpx import ASGITransport, AsyncClient
    from app.core.dependencies import get_db_session, get_llm_service, get_playwright_service
    from app.main import create_app

    app = create_app()
    mock_pw = MockPlaywrightService()
    mock_llm = MockLLMService()
    app.state.playwright = mock_pw

    app.dependency_overrides[get_db_session]         = lambda: db_session
    app.dependency_overrides[get_playwright_service] = lambda: mock_pw
    app.dependency_overrides[get_llm_service]        = lambda: mock_llm

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
