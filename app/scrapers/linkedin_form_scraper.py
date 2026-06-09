"""
LinkedIn Jobs form-filling scraper — Playwright async, non-headless Chromium.

Flow per run
────────────
1.  Launch non-headless Chromium with stealth init scripts.
2.  Navigate to https://www.linkedin.com/jobs/search  (domcontentloaded).
3.  Dismiss cookie banner / sign-in modal if present.
4.  Locate keyword field → triple-click to select all → human-type keywords.
5.  Locate location field → triple-click → human-type location.
6.  Press Enter (or click Submit button).
7.  Wait for networkidle (30 s) or fall back to domcontentloaded + fixed delay.
8.  Detect redirect to /login or /checkpoint → raise LinkedInBlockedError.
9.  Dismiss overlays again (they reappear after navigation).
10. Scroll the result container in steps until height stops growing.
11. Extract every visible job card via fallback selector chains.
12. Parse: title, company, location, URL, posted date, work_mode.
13. Deduplicate by job URL → return up to max_jobs results.

Anti-detection
──────────────
• headless=False                      — real visible browser
• navigator.webdriver hidden          — stealth init script
• human-like typing (50-120 ms/key)  — type() with random delay
• random inter-action delays          — 500-2 500 ms
• realistic User-Agent string         — Chrome 124 on Windows 10
• languages / plugins spoofed        — init scripts
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright,
)

from app.config import get_settings
from app.models.harvest_models import FiltersConfig

logger = structlog.get_logger(__name__)

# Saved session file — written by /linkedin-save-session, read by scraper
LINKEDIN_SESSION_FILE = Path("data/config/linkedin_session.json")


# ══════════════════════════════════════════════════════════════════════════════
# Custom exceptions
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInBlockedError(RuntimeError):
    """Raised when LinkedIn redirects to a login / checkpoint page."""


class LinkedInNoResultsError(RuntimeError):
    """Raised when the results page loads but no job cards are found."""


# ══════════════════════════════════════════════════════════════════════════════
# Result dataclass
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScrapedJob:
    title:     str
    company:   str
    location:  str
    job_url:   str
    posted:    str
    work_mode: str
    job_type:  str


# ══════════════════════════════════════════════════════════════════════════════
# Selector chains  (ordered: most-specific first, broadest last)
# ══════════════════════════════════════════════════════════════════════════════

class _S:
    # ── Search form inputs ────────────────────────────────────────────────────
    KW_INPUT: list[str] = [
        "input#job-search-bar-keywords",
        "input[aria-label*='itle, skill']",
        "input[aria-label*='keyword']",
        "input[aria-label*='earch']",
        "input[placeholder*='itle']",
        "input[placeholder*='keyword']",
        "input.jobs-search-box__text-input[id*='keyword']",
        "input.jobs-search-box__text-input",
        "input[type='text']",
    ]
    LOC_INPUT: list[str] = [
        "input#job-search-bar-location",
        "input[aria-label*='ocation']",
        "input[aria-label*='ity, state']",
        "input[placeholder*='ocation']",
        "input[placeholder*='ity']",
        "input.jobs-search-box__text-input--bordered",
        "input.jobs-search-box__text-input[id*='location']",
    ]
    SUBMIT: list[str] = [
        "button[type='submit'].jobs-search-box__submit-button",
        "button.jobs-search-box__submit-button",
        "button[aria-label*='earch']",
        "button[data-tracking-control-name*='search']",
        "button[type='submit']",
    ]

    # ── Login form ────────────────────────────────────────────────────────────
    LOGIN_EMAIL: list[str] = [
        "input[name='session_key']",
        "input#username",
        "input[autocomplete='username']",
        "input[type='email']",
    ]
    LOGIN_PASSWORD: list[str] = [
        "input[name='session_password']",
        "input#password",
        "input[autocomplete='current-password']",
        "input[type='password']",
    ]
    LOGIN_SUBMIT: list[str] = [
        "button[data-litms-control-urn*='login-submit']",
        "button.btn__primary--large",
        "button[type='submit']",
        "button:has-text('Sign in')",
    ]
    LOGIN_ERROR: list[str] = [
        "div.alert-content",
        "p[role='alert']",
        "#error-for-password",
        "#error-for-username",
        ".form__label--error",
        "span.error",
    ]

    # ── Overlay dismissal ─────────────────────────────────────────────────────
    COOKIE: list[str] = [
        'button[action-type="ACCEPT"]',
        'button[data-control-name="ga-cookie-accept"]',
        'button:has-text("Accept cookies")',
        'button:has-text("Accept")',
    ]
    MODAL_DISMISS: list[str] = [
        'button[data-tracking-control-name="public_jobs_guest-alert-dismiss"]',
        'button.modal__dismiss',
        'button[aria-label="Dismiss"]',
        'div[role="dialog"] button[aria-label="Close"]',
        'button:has-text("Not now")',
        'button:has-text("Skip")',
    ]
    MODAL: list[str] = [
        'div[role="dialog"]',
        'div.authentication-outlet',
        'section.authentication-outlet',
    ]

    # ── Job card container ────────────────────────────────────────────────────
    CONTAINER: list[str] = [
        "ul.jobs-search__results-list",
        "div.jobs-search-results-list",
        "ul[class*='jobs-search-results__list']",
        ".scaffold-layout__list-container",
        ".scaffold-layout__list",
    ]

    # ── Individual card items ─────────────────────────────────────────────────
    CARD: list[str] = [
        "ul.jobs-search__results-list > li",
        "li[data-occludable-job-id]",
        "li.jobs-search-results__list-item",
        "div.base-card",
        "div.job-search-card",
    ]

    # ── Fields within a card ──────────────────────────────────────────────────
    TITLE: list[str] = [
        "h3.base-search-card__title",
        "a.job-card-list__title",
        "span[aria-label]",
        "[class*='job-card'] h3",
        "h3",
    ]
    COMPANY: list[str] = [
        "h4.base-search-card__subtitle",
        "a.job-card-container__company-name",
        "[class*='company-name']",
        "h4 a",
        "h4",
    ]
    LOCATION: list[str] = [
        "span.job-search-card__location",
        "span.job-card-container__metadata-item",
        "[class*='location']",
        "li.job-card-container__metadata-item",
    ]
    LINK: list[str] = [
        "a.base-card__full-link",
        "a[href*='/jobs/view/']",
        "a.job-card-container__link",
        "a[data-tracking-control-name*='job']",
        "a[href*='linkedin.com/jobs']",
    ]
    POSTED: list[str] = [
        "time",
        "span.job-search-card__listdate",
        "[class*='listdate']",
        "span[class*='time']",
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Stealth
# ══════════════════════════════════════════════════════════════════════════════

# ── LinkedIn filter URL param maps ────────────────────────────────────────────
_WORK_MODE_MAP: dict[str, str] = {"Remote": "2", "Hybrid": "3", "Onsite": "1", "Any": ""}
_JOB_TYPE_MAP:  dict[str, str] = {"Contract": "C", "Permanent": "F", "Part-time": "P", "Any": ""}
_DATE_MAP:      dict[int, str] = {24: "r86400", 168: "r604800", 720: "r2592000"}

_STEALTH: list[str] = [
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});",
    "Object.defineProperty(navigator,'plugins',{get:()=>({length:5,0:{name:'Chrome PDF Plugin'},1:{name:'Chrome PDF Viewer'},2:{name:'Native Client'}})});",
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en','en-GB']});",
    "delete window.__playwright; delete window.__pw_manual; delete window.__pw_manual;",
    """
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param){
        if(param===37445) return 'Intel Inc.';
        if(param===37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
        return getParameter.call(this,param);
    };
    """,
]

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

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

_UNICODE_JUNK = re.compile(r"[ ​‌‍﻿­]")
_WHITESPACE   = re.compile(r"[ \t]{2,}")


def _clean(text: str) -> str:
    text = _UNICODE_JUNK.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return re.sub(r"\n+", " ", text).strip()


def _infer_work_mode(location: str) -> str:
    loc = (location or "").lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    if "on-site" in loc or "onsite" in loc:
        return "onsite"
    return "not_specified"


def _format_posted(raw: str) -> str:
    """Normalise posted date to YYYY-MM-DD. Handles ISO datetime and relative text."""
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = raw.strip()
    if "T" in raw and len(raw) >= 10:
        return raw[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    # Relative text like "2 days ago", "1 week ago" — use today
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _delay(page: Page, lo: int, hi: int) -> None:
    """Random wait in [lo, hi] ms — makes action timing non-uniform."""
    await page.wait_for_timeout(random.randint(lo, hi))


async def _first_text(root: Page | ElementHandle, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await root.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


async def _first_attr(root: Page | ElementHandle, selectors: list[str], attr: str) -> str:
    for sel in selectors:
        try:
            el = await root.query_selector(sel)
            if el:
                val = await el.get_attribute(attr)
                if val:
                    return val.strip()
        except Exception:
            continue
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Main scraper
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInFormScraper:
    """
    Async context manager that launches a non-headless Chromium browser,
    fills in the LinkedIn Jobs search form, and extracts job cards.

    Usage::

        async with LinkedInFormScraper() as scraper:
            jobs = await scraper.search(
                keywords = "Python Developer",
                location = "India",
                max_jobs = 5,
                job_type = "Contract",
            )
    """

    def __init__(self) -> None:
        self._pw:               Playwright     | None = None
        self._browser:          Browser        | None = None
        self._context:          BrowserContext | None = None
        self._has_saved_session: bool = False
        _cfg           = get_settings()
        self._email:    str = _cfg.linkedin_email
        self._password: str = _cfg.linkedin_password

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "LinkedInFormScraper":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless = False,
            slow_mo  = 0,
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
        # Load saved session cookies if present (avoids re-login + 2FA)
        if LINKEDIN_SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(LINKEDIN_SESSION_FILE)
            self._has_saved_session = True
            logger.info("linkedin_session_loaded", path=str(LINKEDIN_SESSION_FILE))
        else:
            logger.info("linkedin_no_saved_session_will_login")

        self._context = await self._browser.new_context(**ctx_kwargs)
        for script in _STEALTH:
            await self._context.add_init_script(script)
        logger.info("browser_launched", headless=False, saved_session=self._has_saved_session)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("browser_closed")

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search(self, filters: FiltersConfig) -> list[ScrapedJob]:
        """
        Open LinkedIn Jobs, fill the search form, scroll, extract cards.

        Raises
        ──────
        LinkedInBlockedError    — redirected to /login or /checkpoint
        LinkedInNoResultsError  — page loaded but no cards found
        Any other exception     — network/browser failure
        """
        assert self._context, "Use LinkedInFormScraper as an async context manager"
        page = await self._context.new_page()
        try:
            return await self._run(page, filters)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ── Internal flow ──────────────────────────────────────────────────────────

    async def _run(self, page: Page, f: FiltersConfig) -> list[ScrapedJob]:

        # ── Step 0: Login or verify saved session ─────────────────────────────
        if self._has_saved_session:
            # Verify the saved session is still alive
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20_000)
            await _delay(page, 1_500, 2_500)
            if any(p in page.url for p in ("/login", "/authwall", "/checkpoint")):
                logger.warning("linkedin_session_expired_falling_back_to_credential_login")
                self._has_saved_session = False
                if self._email and self._password:
                    await self._login(page)
                    await _delay(page, 1_000, 2_000)
            else:
                logger.info("linkedin_session_valid", url=page.url)
        elif self._email and self._password:
            await self._login(page)
            await _delay(page, 1_000, 2_000)

        # ── Step 1: Navigate ──────────────────────────────────────────────────
        logger.info("navigating", url="https://www.linkedin.com/jobs/search")
        try:
            await page.goto(
                "https://www.linkedin.com/jobs/search",
                wait_until = "domcontentloaded",
                timeout    = 30_000,
            )
        except Exception as exc:
            logger.error("initial_navigation_failed", error=str(exc))
            raise

        await _delay(page, 1_500, 2_500)
        await self._check_blocked(page)
        await self._dismiss_overlays(page)

        # ── Step 2: Fill keyword field ────────────────────────────────────────
        logger.info("filling_keyword", value=f.keyword)
        kw_filled = await self._fill(page, _S.KW_INPUT, f.keyword)
        if not kw_filled:
            logger.warning("keyword_field_not_found")
        await _delay(page, 600, 1_000)

        # ── Step 3: Fill location field ───────────────────────────────────────
        logger.info("filling_location", value=f.location)
        loc_filled = await self._fill(page, _S.LOC_INPUT, f.location)
        if not loc_filled:
            logger.warning("location_field_not_found")
        await _delay(page, 800, 1_200)

        # ── Step 4: Submit search ─────────────────────────────────────────────
        logger.info("submitting_search")
        await self._submit(page)

        # ── Step 5: Wait for results ──────────────────────────────────────────
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            logger.debug("networkidle_timeout_fallback")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            await _delay(page, 2_000, 3_000)

        await self._check_blocked(page)
        logger.info("results_page_loaded", url=page.url)

        # ── Step 5b: Navigate to filtered URL (work_mode, job_type, date) ─────
        filtered_url = self._build_filtered_url(
            f.keyword, f.location, f.job_type, f.work_mode, f.search_window_hours
        )
        logger.info("applying_filters", work_mode=f.work_mode, job_type=f.job_type,
                    search_window_hours=f.search_window_hours, url=filtered_url)
        await page.goto(filtered_url, wait_until="domcontentloaded", timeout=30_000)
        await _delay(page, 1_500, 2_500)
        await self._check_blocked(page)

        await _delay(page, 1_000, 1_800)
        await self._dismiss_overlays(page)

        # ── Step 6: Scroll to reveal all virtual-DOM cards ────────────────────
        await self._scroll_results(page)

        # ── Step 7: Extract job cards ─────────────────────────────────────────
        jobs = await self._extract_cards(page, f.max_jobs, f.job_type)

        if not jobs:
            raise LinkedInNoResultsError(
                f"No job cards found for '{f.keyword}' in '{f.location}'"
            )

        logger.info("extraction_complete", count=len(jobs))
        return jobs

    # ── Form interaction ───────────────────────────────────────────────────────

    async def _fill(self, page: Page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                # Wait for element to be in DOM and visible (more reliable than is_visible poll)
                await page.wait_for_selector(sel, state="visible", timeout=5_000)
                loc = page.locator(sel).first
                await loc.scroll_into_view_if_needed()
                await loc.click()
                await _delay(page, 200, 400)
                await loc.press("Control+a")
                await _delay(page, 100, 200)
                await loc.fill("")
                await _delay(page, 150, 250)
                await loc.type(value, delay=random.randint(50, 120))
                await _delay(page, 300, 600)
                logger.debug("field_filled", selector=sel, value=value)
                return True
            except Exception as exc:
                logger.debug("fill_attempt_failed", selector=sel, error=str(exc))
                continue

        # Last-resort: page.fill() directly (bypasses visibility check)
        for sel in selectors:
            try:
                await page.fill(sel, value, timeout=3_000)
                logger.debug("field_filled_direct", selector=sel, value=value)
                return True
            except Exception:
                continue
        return False

    async def _submit(self, page: Page) -> None:
        # Try clicking a submit button
        for sel in _S.SUBMIT:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    logger.debug("submit_clicked", selector=sel)
                    return
            except Exception:
                continue
        # Fallback: press Enter in the last-filled field
        try:
            await page.keyboard.press("Enter")
            logger.debug("submit_via_enter")
        except Exception as exc:
            logger.warning("submit_failed", error=str(exc))

    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        # Cookie banner
        for sel in _S.COOKIE:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_500):
                    await el.click()
                    await _delay(page, 500, 800)
                    logger.debug("cookie_dismissed", selector=sel)
                    break
            except Exception:
                continue

        # Sign-in modal dismiss button
        for sel in _S.MODAL_DISMISS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_500):
                    await el.click()
                    await _delay(page, 500, 800)
                    logger.debug("modal_dismissed", selector=sel)
                    break
            except Exception:
                continue

        # Escape any remaining modal
        for sel in _S.MODAL:
            try:
                if await page.locator(sel).first.is_visible(timeout=800):
                    await page.keyboard.press("Escape")
                    await _delay(page, 400, 700)
                    break
            except Exception:
                continue

    # ── Scrolling ──────────────────────────────────────────────────────────────

    async def _scroll_results(self, page: Page) -> None:
        """Scroll the result container until its height stops growing."""
        container: ElementHandle | None = None
        for sel in _S.CONTAINER:
            try:
                el = await page.query_selector(sel)
                if el:
                    container = el
                    logger.debug("container_found", selector=sel)
                    break
            except Exception:
                continue

        if container:
            prev_height = -1
            for _ in range(25):
                height = await container.evaluate("el => el.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height
                await container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                await _delay(page, 400, 700)
        else:
            # Fallback: scroll the window
            logger.debug("no_container_found_scrolling_window")
            prev_height = -1
            for _ in range(15):
                height = await page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _delay(page, 500, 900)

    # ── Card extraction ────────────────────────────────────────────────────────

    async def _extract_cards(
        self,
        page:     Page,
        max_jobs: int,
        job_type: str,
    ) -> list[ScrapedJob]:
        # Wait for at least one card selector to appear
        for sel in _S.CARD:
            try:
                await page.wait_for_selector(sel, timeout=10_000)
                logger.debug("card_selector_matched", selector=sel)
                break
            except Exception:
                continue

        # Collect raw card elements (try each selector until we get hits)
        raw: list[ElementHandle] = []
        for sel in _S.CARD:
            raw = await page.query_selector_all(sel)
            if raw:
                logger.info("cards_found", selector=sel, count=len(raw))
                break

        seen_urls: set[str] = set()
        jobs: list[ScrapedJob] = []

        for el in raw:
            if len(jobs) >= max_jobs:
                break
            job = await self._parse_card(el, job_type)
            if job and job.job_url and job.job_url not in seen_urls:
                seen_urls.add(job.job_url)
                jobs.append(job)

        return jobs

    async def _parse_card(
        self,
        el:       ElementHandle,
        job_type: str,
    ) -> ScrapedJob | None:
        try:
            title   = await _first_text(el, _S.TITLE)
            company = await _first_text(el, _S.COMPANY)
            loc     = await _first_text(el, _S.LOCATION)
            href    = await _first_attr(el, _S.LINK, "href")

            # <time datetime="2024-05-27T..."> or relative text
            posted_raw = (
                await _first_attr(el, _S.POSTED, "datetime")
                or await _first_text(el, _S.POSTED)
            )

            if not title:
                return None

            # Strip tracking params from URL
            clean_url = href.split("?")[0] if href and "?" in href else href

            return ScrapedJob(
                title     = _clean(title),
                company   = _clean(company) or "Unknown Company",
                location  = _clean(loc)     or "Unknown Location",
                job_url   = clean_url or "",
                posted    = _format_posted(posted_raw),
                work_mode = _infer_work_mode(loc),
                job_type  = job_type,
            )
        except Exception as exc:
            logger.debug("card_parse_error", error=str(exc))
            return None

    # ── Filter URL builder ─────────────────────────────────────────────────────

    @staticmethod
    def _build_filtered_url(
        keywords:            str,
        location:            str,
        job_type:            str,
        work_mode:           str,
        search_window_hours: int,
    ) -> str:
        """Build a LinkedIn Jobs search URL with all active filter params."""
        from urllib.parse import urlencode
        params: dict[str, str] = {
            "keywords": keywords,
            "location": location,
            "sortBy":   "DD",
        }
        wt  = _WORK_MODE_MAP.get(work_mode, "")
        jt  = _JOB_TYPE_MAP.get(job_type, "")
        tpr = _DATE_MAP.get(search_window_hours, "")
        if wt:  params["f_WT"]  = wt
        if jt:  params["f_JT"]  = jt
        if tpr: params["f_TPR"] = tpr
        return f"https://www.linkedin.com/jobs/search?{urlencode(params)}"

    # ── Login ──────────────────────────────────────────────────────────────────

    async def _login(self, page: Page) -> None:
        """Authenticate with LinkedIn credentials from settings."""
        logger.info("linkedin_login_started", email=self._email)

        await page.goto(
            "https://www.linkedin.com/login",
            wait_until = "domcontentloaded",
            timeout    = 30_000,
        )
        await _delay(page, 2_000, 3_000)
        await self._screenshot_failure(page, "linkedin_login_page")

        # Fill email — try label/placeholder-based locators first, then CSS selectors
        email_filled = await self._fill_login_field(
            page,
            label_texts    = ["Email or phone", "Email"],
            placeholders   = ["Email or phone", "Email"],
            css_selectors  = _S.LOGIN_EMAIL,
            value          = self._email,
        )
        if not email_filled:
            await self._screenshot_failure(page, "login_email_field_missing")
            logger.error("linkedin_login_failed", reason="email field not found")
            raise LinkedInBlockedError("LinkedIn login: email input field not found")
        await _delay(page, 400, 700)

        # Fill password
        password_filled = await self._fill_login_field(
            page,
            label_texts    = ["Password"],
            placeholders   = ["Password"],
            css_selectors  = _S.LOGIN_PASSWORD,
            value          = self._password,
        )
        if not password_filled:
            await self._screenshot_failure(page, "login_password_field_missing")
            logger.error("linkedin_login_failed", reason="password field not found")
            raise LinkedInBlockedError("LinkedIn login: password input field not found")
        await _delay(page, 400, 700)

        # Submit
        await self._submit_login(page)

        # Wait for post-login navigation — give LinkedIn time to redirect
        try:
            await page.wait_for_url(
                lambda url: "/login" not in url,
                timeout=15_000,
            )
        except Exception:
            # URL didn't change — may be error page, challenge, or slow network
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
        await _delay(page, 1_500, 2_500)

        current_url = page.url
        _BLOCKED = ("/login", "/checkpoint", "/challenge", "/authwall")
        if any(p in current_url for p in _BLOCKED):
            # Try to read the error message LinkedIn shows
            error_text = ""
            for sel in _S.LOGIN_ERROR:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        error_text = (await el.inner_text()).strip()
                        break
                except Exception:
                    continue
            await self._screenshot_failure(page, "login_failed")
            logger.error(
                "linkedin_login_failed",
                url=current_url,
                reason=error_text or "redirected to gated page after submit",
            )
            raise LinkedInBlockedError(
                f"LinkedIn login failed — landed on {current_url}. "
                f"Reason: {error_text or 'unknown'}"
            )

        logger.info("linkedin_login_success", url=current_url)

    async def _fill_login_field(
        self,
        page:          Page,
        label_texts:   list[str],
        placeholders:  list[str],
        css_selectors: list[str],
        value:         str,
    ) -> bool:
        """
        Five strategies to fill a React-controlled LinkedIn login input.

        1. force=True fill via CSS selector (bypasses Playwright actionability checks)
        2. get_by_label with force fill
        3. get_by_placeholder with force fill
        4. React-native JS setter (nativeInputValueSetter) + keyboard reinforce
        5. Focus-by-JS then keyboard.type
        """
        # Strategy 1: force fill by CSS (bypasses visibility/actionability checks)
        for sel in css_selectors:
            try:
                loc = page.locator(sel).first
                await loc.fill(value, force=True, timeout=4_000)
                # Verify value actually stuck
                actual = await loc.input_value(timeout=2_000)
                if actual == value:
                    logger.debug("login_field_filled_force_css", selector=sel)
                    return True
            except Exception:
                continue

        # Strategy 2: get_by_label + force fill
        for label in label_texts:
            try:
                loc = page.get_by_label(label, exact=False).first
                await loc.fill(value, force=True, timeout=4_000)
                actual = await loc.input_value(timeout=2_000)
                if actual == value:
                    logger.debug("login_field_filled_force_label", label=label)
                    return True
            except Exception:
                continue

        # Strategy 3: get_by_placeholder + force fill
        for ph in placeholders:
            try:
                loc = page.get_by_placeholder(ph, exact=False).first
                await loc.fill(value, force=True, timeout=4_000)
                actual = await loc.input_value(timeout=2_000)
                if actual == value:
                    logger.debug("login_field_filled_force_placeholder", placeholder=ph)
                    return True
            except Exception:
                continue

        # Strategy 4: React-native JS setter (survives React's controlled-input reset)
        react_js = """
            ([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                el.focus();
                nativeSetter.call(el, val);
                ['input','change'].forEach(evt =>
                    el.dispatchEvent(new Event(evt, {bubbles:true, cancelable:true}))
                );
                return el.value === val;
            }
        """
        for sel in css_selectors:
            try:
                ok = await page.evaluate(react_js, [sel, value])
                if ok:
                    logger.debug("login_field_filled_react_js", selector=sel)
                    return True
            except Exception:
                continue

        # Strategy 5: focus-by-JS then keyboard.type (most reliable for React SPAs)
        focus_js = "([sel]) => { const el = document.querySelector(sel); if(el){el.focus();el.click();return true;} return false; }"
        for sel in css_selectors:
            try:
                ok = await page.evaluate(focus_js, [sel])
                if ok:
                    await _delay(page, 200, 350)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Delete")
                    await _delay(page, 100, 200)
                    await page.keyboard.type(value, delay=random.randint(60, 110))
                    await _delay(page, 200, 400)
                    logger.debug("login_field_filled_keyboard", selector=sel)
                    return True
            except Exception:
                continue

        return False

    async def _submit_login(self, page: Page) -> None:
        # Try role-based locator first (most semantic / reliable)
        try:
            btn = page.get_by_role("button", name="Sign in", exact=True).first
            await btn.click(force=True, timeout=4_000)
            logger.debug("login_submit_clicked_role")
            return
        except Exception:
            pass

        # Try CSS selectors with force=True (bypasses actionability)
        for sel in _S.LOGIN_SUBMIT:
            try:
                btn = page.locator(sel).first
                await btn.click(force=True, timeout=3_000)
                logger.debug("login_submit_clicked_force", selector=sel)
                return
            except Exception:
                continue

        # Last resort: JS click on first submit button
        try:
            ok = await page.evaluate("""
                () => {
                    const btn = document.querySelector('button[type="submit"]')
                           || document.querySelector('button.btn__primary--large')
                           || Array.from(document.querySelectorAll('button'))
                                    .find(b => /sign.?in/i.test(b.textContent));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """)
            if ok:
                logger.debug("login_submit_via_js_click")
                return
        except Exception:
            pass

        await page.keyboard.press("Enter")
        logger.debug("login_submit_via_enter")

    async def _screenshot_failure(self, page: Page, label: str) -> None:
        """Save a debug screenshot to data/results/linkedin/."""
        try:
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = Path("data/results/linkedin") / f"{label}_{ts}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
            logger.info("screenshot_saved", path=str(path))
        except Exception as exc:
            logger.debug("screenshot_failed", error=str(exc))

    # ── Blocked detection ──────────────────────────────────────────────────────

    async def _check_blocked(self, page: Page) -> None:
        blocked_patterns = ("/login", "/checkpoint", "/challenge", "/authwall")
        for pat in blocked_patterns:
            if pat in page.url:
                await self._screenshot_failure(page, "blocked_redirect")
                logger.error(
                    "linkedin_login_failed",
                    url=page.url,
                    reason=f"redirected to {pat} during harvest",
                )
                raise LinkedInBlockedError(
                    f"LinkedIn redirected to a gated page: {page.url}"
                )
