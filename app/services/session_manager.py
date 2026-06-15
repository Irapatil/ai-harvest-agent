"""
Centralized session persistence for all scraper agents.

Each source (linkedin, naukri, dice) gets its own session JSON file in
data/sessions/. The SessionManager checks whether a saved session is still
valid by navigating to the source's auth-check URL and looking for an
authenticated indicator element. If the session is expired or missing, the
caller is expected to log in and then call save_session().

Usage::

    sm = SessionManager("naukri")
    storage_arg = sm.storage_state_arg()          # None when no file exists

    async with BrowserManager(storage_state=storage_arg) as bm:
        page = await bm.new_page()
        if not await sm.is_session_valid(page):
            await agent._login(page)
            await sm.save_session(page)
        # proceed with scraping ...
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog
from playwright.async_api import Page

logger = structlog.get_logger(__name__)

_SESSIONS_DIR = Path("data/sessions")

# Per-source configuration
_SOURCE_CONFIG: dict[str, dict] = {
    "linkedin": {
        "session_file": "linkedin_session.json",
        "auth_check_url": "https://www.linkedin.com/feed/",
        "auth_selectors": [
            "nav[aria-label='Primary']",
            "div.global-nav__content",
            "ul.global-nav__primary-items",
            "nav.global-nav",
            "img.global-nav__me-photo",
        ],
        "gated_paths": ["/login", "/checkpoint", "/challenge", "/authwall", "/uas/"],
    },
    "naukri": {
        "session_file": "naukri_session.json",
        "auth_check_url": "https://www.naukri.com/",
        "auth_selectors": [
            "a[href*='/mnjuser/profile']",
            "a[href*='/mnjuser/homepage']",
            ".nI-gNb-drawer__icon",
            "a[data-ga-track*='profile']",
            "span[class*='nI-gNb-user']",
            "div.header-avatar",
        ],
        "gated_paths": ["/employer-login", "/nlogin/", "/challenge", "/recruit/login"],
    },
    "dice": {
        "session_file": "dice_session.json",
        "auth_check_url": "https://www.dice.com/dashboard",
        "auth_selectors": [
            "a[href*='/dashboard']",
            "nav a[href*='/profile']",
            "button[data-testid='sign-out']",
            ".profile-link",
            "a[href*='/user/profile']",
        ],
        "gated_paths": ["/dashboard/login", "/login", "/register"],
    },
}


class SessionManager:
    """
    Manages Playwright storage-state sessions for a single source.

    Parameters
    ──────────
    source   One of "linkedin", "naukri", "dice".
    """

    def __init__(self, source: str) -> None:
        if source not in _SOURCE_CONFIG:
            raise ValueError(f"Unknown source '{source}'. Valid: {list(_SOURCE_CONFIG)}")
        self._source  = source
        self._cfg     = _SOURCE_CONFIG[source]
        self._path    = _SESSIONS_DIR / self._cfg["session_file"]
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ───────────────────────────────────────────────────────────

    @property
    def session_path(self) -> Path:
        return self._path

    def session_exists(self) -> bool:
        """Return True if a session file exists on disk and is non-empty."""
        return self._path.exists() and self._path.stat().st_size > 10

    def storage_state_arg(self) -> str | None:
        """
        Return the path string to pass as `storage_state` to BrowserManager,
        or None if no session file exists.
        """
        return str(self._path) if self.session_exists() else None

    # ── Session validation ─────────────────────────────────────────────────────

    async def is_session_valid(self, page: Page) -> bool:
        """
        Navigate to the source's auth-check URL and verify authenticated
        indicator elements are present. Returns True if the session is live.
        """
        cfg = self._cfg
        try:
            await page.goto(
                cfg["auth_check_url"],
                wait_until="domcontentloaded",
                timeout=25_000,
            )
            await page.wait_for_timeout(2_000)
        except Exception as exc:
            logger.warning(
                "session_check_navigation_failed",
                source=self._source,
                error=str(exc),
            )
            return False

        # Reject if on a gated/login page
        current_url = page.url
        for pat in cfg["gated_paths"]:
            if pat in current_url:
                logger.info(
                    "session_expired_gated_redirect",
                    source=self._source,
                    url=current_url,
                )
                return False

        # Check for at least one authenticated indicator
        for sel in cfg["auth_selectors"]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info(
                        "session_valid",
                        source=self._source,
                        indicator=sel,
                    )
                    return True
            except Exception:
                continue

        logger.info(
            "session_invalid_no_auth_indicator",
            source=self._source,
            url=current_url,
        )
        return False

    # ── Session persistence ────────────────────────────────────────────────────

    async def save_session(self, page: Page) -> None:
        """Persist the current browser context's cookies + storage to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await page.context.storage_state(path=str(self._path))
            size = self._path.stat().st_size
            logger.info(
                "session_saved",
                source=self._source,
                path=str(self._path),
                bytes=size,
            )
        except Exception as exc:
            logger.error(
                "session_save_failed",
                source=self._source,
                error=str(exc),
            )

    def clear_session(self) -> None:
        """Delete the session file (forces re-login on next run)."""
        if self._path.exists():
            self._path.unlink()
            logger.info("session_cleared", source=self._source, path=str(self._path))
        else:
            logger.debug("session_clear_noop_file_not_found", source=self._source)

    def session_info(self) -> dict:
        """Return a dict describing current session state (for API inspection)."""
        exists = self.session_exists()
        info: dict = {
            "source":       self._source,
            "session_file": str(self._path),
            "exists":       exists,
        }
        if exists:
            import datetime as _dt
            mtime = self._path.stat().st_mtime
            info["last_saved"] = _dt.datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
            info["size_bytes"]  = self._path.stat().st_size
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                info["cookie_count"] = len(data.get("cookies", []))
            except Exception:
                pass
        return info
