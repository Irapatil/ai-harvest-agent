"""Playwright browser pool and page action helpers."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.config import Settings
from app.core.exceptions import PlaywrightError

logger = structlog.get_logger(__name__)


@dataclass
class PageSnapshot:
    url: str
    title: str
    html: str
    text: str
    screenshot_b64: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)


class PlaywrightService:
    """Manages a pool of Playwright browser contexts for concurrent harvesting."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore: asyncio.Semaphore | None = None

    async def start(self) -> None:
        """Launch browser and initialize concurrency semaphore."""
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self._settings.playwright_browser)
        self._browser = await launcher.launch(headless=self._settings.playwright_headless)
        self._semaphore = asyncio.Semaphore(self._settings.playwright_pool_size)
        logger.info(
            "browser_launched",
            browser=self._settings.playwright_browser,
            headless=self._settings.playwright_headless,
        )

    async def stop(self) -> None:
        """Close browser and Playwright."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_stopped")

    # ── Context manager for pages ─────────────────────────────────────────────────

    async def _new_context(self) -> BrowserContext:
        assert self._browser, "PlaywrightService not started"
        return await self._browser.new_context(
            viewport={
                "width": self._settings.playwright_viewport_width,
                "height": self._settings.playwright_viewport_height,
            },
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )

    # ── High-level page actions ───────────────────────────────────────────────────

    async def navigate(self, url: str, wait_until: str = "networkidle") -> PageSnapshot:
        """Navigate to a URL and return a full page snapshot."""
        assert self._semaphore, "PlaywrightService not started"
        async with self._semaphore:
            context = await self._new_context()
            try:
                page = await context.new_page()
                await page.goto(
                    url,
                    timeout=self._settings.playwright_timeout_ms,
                    wait_until=wait_until,  # type: ignore[arg-type]
                )
                return await self._snapshot(page)
            except Exception as exc:
                raise PlaywrightError(str(exc), url=url) from exc
            finally:
                await context.close()

    async def click_and_snapshot(self, url: str, selector: str) -> PageSnapshot:
        """Navigate to URL, click an element, return resulting snapshot."""
        assert self._semaphore
        async with self._semaphore:
            context = await self._new_context()
            try:
                page = await context.new_page()
                await page.goto(url, timeout=self._settings.playwright_timeout_ms)
                await page.click(selector, timeout=self._settings.playwright_timeout_ms)
                await page.wait_for_load_state("networkidle")
                return await self._snapshot(page)
            except Exception as exc:
                raise PlaywrightError(str(exc), url=url) from exc
            finally:
                await context.close()

    async def fill_and_submit(
        self,
        url: str,
        fields: dict[str, str],
        submit_selector: str,
    ) -> PageSnapshot:
        """Fill a form and submit it, return snapshot of result page."""
        assert self._semaphore
        async with self._semaphore:
            context = await self._new_context()
            try:
                page = await context.new_page()
                await page.goto(url, timeout=self._settings.playwright_timeout_ms)
                for selector, value in fields.items():
                    await page.fill(selector, value)
                await page.click(submit_selector)
                await page.wait_for_load_state("networkidle")
                return await self._snapshot(page)
            except Exception as exc:
                raise PlaywrightError(str(exc), url=url) from exc
            finally:
                await context.close()

    async def scroll_and_snapshot(self, url: str, scroll_count: int = 3) -> PageSnapshot:
        """Scroll a page incrementally (for infinite scroll), return snapshot."""
        assert self._semaphore
        async with self._semaphore:
            context = await self._new_context()
            try:
                page = await context.new_page()
                await page.goto(url, timeout=self._settings.playwright_timeout_ms)
                for _ in range(scroll_count):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(0.8)
                return await self._snapshot(page)
            except Exception as exc:
                raise PlaywrightError(str(exc), url=url) from exc
            finally:
                await context.close()

    async def execute_js(self, url: str, script: str) -> Any:
        """Navigate to URL and execute arbitrary JavaScript, return result."""
        assert self._semaphore
        async with self._semaphore:
            context = await self._new_context()
            try:
                page = await context.new_page()
                await page.goto(url, timeout=self._settings.playwright_timeout_ms)
                return await page.evaluate(script)
            except Exception as exc:
                raise PlaywrightError(str(exc), url=url) from exc
            finally:
                await context.close()

    # ── Snapshot helper ───────────────────────────────────────────────────────────

    async def _snapshot(self, page: Page) -> PageSnapshot:
        html = await page.content()
        title = await page.title()
        url = page.url

        # Plain text via JS
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )

        # Extract all links
        links_raw = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.innerText.trim(),
                href: a.href
            })).filter(l => l.href.startsWith('http'))"""
        )

        # Extract form structures
        forms_raw = await page.evaluate(
            """() => Array.from(document.forms).map(f => ({
                action: f.action,
                method: f.method,
                fields: Array.from(f.elements).map(e => ({
                    name: e.name,
                    type: e.type,
                    placeholder: e.placeholder || ''
                }))
            }))"""
        )

        # Screenshot as base64
        screenshot_bytes = await page.screenshot(full_page=False)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

        return PageSnapshot(
            url=url,
            title=title,
            html=html[:50_000],      # cap at 50 KB for LLM context
            text=text[:20_000],
            screenshot_b64=screenshot_b64,
            links=links_raw[:100],   # top 100 links
            forms=forms_raw,
        )
