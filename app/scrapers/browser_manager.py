"""
Generic Playwright browser lifecycle manager with anti-detection stealth patches.

BrowserManager is an async context manager that owns one browser session.
Every module that needs a browser should acquire it through here — never
launch Playwright directly in agent or route code.

Usage::

    async with BrowserManager(headless=False) as bm:
        page = await bm.new_page()
        await page.goto("https://www.linkedin.com/jobs")
"""
from __future__ import annotations

from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = structlog.get_logger(__name__)


# ── Browser fingerprint constants ─────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

_STEALTH_SCRIPTS: list[str] = [
    # Hide navigator.webdriver
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});",

    # Fake a populated plugins list
    """Object.defineProperty(navigator,'plugins',{
        get:()=>({length:5,
            0:{name:'Chrome PDF Plugin'},
            1:{name:'Chrome PDF Viewer'},
            2:{name:'Native Client'},
            3:{name:'Widevine'},
            4:{name:'MetaMask'}
        })
    });""",

    # Realistic language preferences
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en','en-GB']});",

    # Spoof WebGL renderer (headless fingerprint)
    """const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p){
        if(p===37445) return 'Intel Inc.';
        if(p===37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
        return _getParam.call(this,p);
    };""",

    # Remove Playwright-specific window properties
    "delete window.__playwright; delete window.__pw_manual;",
]


# ══════════════════════════════════════════════════════════════════════════════
# BrowserManager
# ══════════════════════════════════════════════════════════════════════════════

class BrowserManager:
    """
    Owns one Playwright Chromium session for the lifetime of an `async with` block.

    Parameters
    ──────────
    headless    Run without a visible window (False = visible, better anti-detect).
    slow_mo     Extra ms delay between Playwright actions (0 for maximum speed).
    """

    def __init__(self, headless: bool = False, slow_mo: int = 0, storage_state: str | None = None) -> None:
        self._headless:      bool               = headless
        self._slow_mo:       int                = slow_mo
        self._storage_state: str | None         = storage_state
        self._pw:            Playwright    | None = None
        self._browser:       Browser       | None = None
        self._context:       BrowserContext | None = None

    async def __aenter__(self) -> "BrowserManager":
        self._pw = await async_playwright().start()

        self._browser = await self._pw.chromium.launch(
            headless = self._headless,
            slow_mo  = self._slow_mo,
            args     = _LAUNCH_ARGS,
        )

        ctx_kwargs: dict = dict(
            viewport            = {"width": 1366, "height": 900},
            user_agent          = _USER_AGENT,
            locale              = "en-US",
            timezone_id         = "Europe/London",
            color_scheme        = "light",
            java_script_enabled = True,
        )
        if self._storage_state:
            ctx_kwargs["storage_state"] = self._storage_state

        self._context = await self._browser.new_context(**ctx_kwargs)

        for script in _STEALTH_SCRIPTS:
            await self._context.add_init_script(script)

        logger.info("browser_started", headless=self._headless, slow_mo=self._slow_mo,
                    session_loaded=bool(self._storage_state))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        logger.info("browser_stopped")

    async def new_page(self) -> Page:
        """Open and return a fresh browser tab."""
        if not self._context:
            raise RuntimeError("BrowserManager must be used as an async context manager")
        return await self._context.new_page()
