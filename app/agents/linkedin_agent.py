"""
LinkedIn Harvest Agent — employer-authenticated job scraper.

Rules
─────
• NEVER fall back to guest/anonymous mode.
• Session file (data/config/linkedin_session.json) is always tried first.
• If the session is expired and credentials are available, re-login.
• If login fails for any reason → raise LinkedInLoginError immediately.
• Debug screenshots are saved to debug/ at every stage.
• HTML snapshots are saved to debug/ on any error.

Credentials
───────────
Loaded from .env via Settings (LINKEDIN_EMAIL / LINKEDIN_PASSWORD).
Never hardcoded anywhere in this file.

Filter URL parameters
─────────────────────
work_mode  Remote → f_WT=2  |  Hybrid → f_WT=3  |  Onsite → f_WT=1
job_type   Contract → f_JT=C  |  Permanent → f_JT=F  |  Part-time → f_JT=P
date       24h → r86400  |  week → r604800  |  month → r2592000
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import structlog
from playwright.async_api import ElementHandle, Page

from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import PersistentBrowserManager

logger = structlog.get_logger(__name__)

LINKEDIN_SESSION_FILE = Path("data/sessions/linkedin_session.json")
_DEBUG_DIR            = Path("debug")

_LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs/search/?"

# URLs that indicate we are NOT logged in
_GATED_PATHS = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/", "login.live.com", "login.microsoftonline.com")


# ── Custom exception ───────────────────────────────────────────────────────────

class LinkedInLoginError(RuntimeError):
    """Raised when LinkedIn authentication fails. Scraping is aborted."""


# ── Filter → URL param maps ───────────────────────────────────────────────────

_WORK_MODE_MAP: dict[str, str] = {
    "Remote": "2",
    "Hybrid": "3",
    "Onsite": "1",
    "Any":    "",
}
_JOB_TYPE_MAP: dict[str, str] = {
    "Contract":   "C",
    "Permanent":  "F",
    "Part-time":  "P",
    "Full-time":  "F",
    "Freelance":  "T",
    "Any":        "",
}
_DATE_MAP: dict[int, str] = {
    24:  "r86400",
    168: "r604800",
    720: "r2592000",
}


# ── Scraped job dataclass ─────────────────────────────────────────────────────

@dataclass
class LinkedInScrapedJob:
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    skills:          list[str] = field(default_factory=list)
    work_mode:       str       = "not_specified"
    company_url:     str       = ""
    employment_type: str       = ""
    source:          str       = "LinkedIn"


# ── CSS selector fallback chains ──────────────────────────────────────────────

class _Sel:
    # Login form
    LOGIN_EMAIL: list[str] = [
        "input#username",
        "input[name='session_key']",
        "input[autocomplete='username']",
        "input[type='email']",
        "input[type='text']",
    ]
    LOGIN_PASSWORD: list[str] = [
        "input#password",
        "input[name='session_password']",
        "input[autocomplete='current-password']",
        "input[type='password']",
    ]
    LOGIN_SUBMIT: list[str] = [
        "button[type='submit'][data-litms-control-urn*='sign-in']",
        "button[type='submit'].btn__primary--large",
        "button[type='submit']",
        "button:has-text('Sign in')",
    ]

    # Authenticated nav indicators
    AUTH_AVATAR: list[str] = [
        "img.global-nav__me-photo",
        "img[class*='global-nav__me-photo']",
        "[data-control-name='nav.settings']",
        "a[href*='/in/'][aria-label]",
    ]
    AUTH_NAV: list[str] = [
        "nav[aria-label='Primary']",
        "div.global-nav__content",
        "ul.global-nav__primary-items",
        "nav.global-nav",
    ]

    # Overlays
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
    ]

    # Result container + cards (authenticated view)
    CONTAINER: list[str] = [
        "div.jobs-search-results-list",
        "ul.jobs-search__results-list",
        "ul[class*='jobs-search-results__list']",
        ".scaffold-layout__list-container",
        ".scaffold-layout__list",
    ]
    CARD: list[str] = [
        "li[data-occludable-job-id]",
        "li.jobs-search-results__list-item",
        "ul.jobs-search__results-list > li",
        "div.base-card",
        "li[class*='jobs-search-results']",
    ]

    # Card list-view fields
    TITLE:    list[str] = ["a.job-card-list__title", "h3.base-search-card__title", "h3", "h2"]
    COMPANY:  list[str] = ["a.job-card-container__company-name", "h4.base-search-card__subtitle", "h4 a", "h4"]
    LOCATION: list[str] = ["span.job-card-container__metadata-item", "span.job-search-card__location", "[class*='location']"]
    LINK:     list[str] = ["a.job-card-list__title", "a.base-card__full-link", "a[href*='/jobs/view/']"]
    POSTED:   list[str] = ["time", "span.job-search-card__listdate", "[class*='listdate']"]

    # Detail panel (opened after clicking a card)
    DETAIL_PANEL:      list[str] = ["#job-details", "div.jobs-description", "article.jobs-description"]
    DETAIL_TITLE:      list[str] = [".jobs-unified-top-card__job-title", "h1.jobs-unified-top-card__job-title", "h1"]
    DETAIL_COMPANY:    list[str] = [".jobs-unified-top-card__company-name a", ".jobs-unified-top-card__company-name", "a[data-tracking-control-name='public_jobs_topcard-org-name']"]
    DETAIL_COMPANY_URL: list[str] = [".jobs-unified-top-card__company-name a"]
    DETAIL_LOCATION:   list[str] = [".jobs-unified-top-card__bullet", ".jobs-unified-top-card__workplace-type", ".topcard__flavor"]
    DETAIL_POSTED:     list[str] = ["span.jobs-unified-top-card__posted-date", ".topcard__flavor--metadata time", "time"]
    DETAIL_EMP_TYPE:   list[str] = [".jobs-unified-top-card__job-insight span", ".jobs-unified-top-card__workplace-type"]
    DETAIL_SALARY:     list[str] = [".jobs-unified-top-card__job-insight--highlight", "[class*='salary']", "[class*='compensation']"]
    DETAIL_SKILLS:     list[str] = [".job-details-skill-match-status-list li", ".jobs-unified-top-card__job-insight ul li"]
    DETAIL_DESC:       list[str] = ["#job-details", "div.jobs-description__content", "div.jobs-description"]


# ── Helpers ───────────────────────────────────────────────────────────────────

_UNICODE_JUNK = re.compile(r"[ ​‌‍﻿­]")
_WHITESPACE   = re.compile(r"[ \t]{2,}")


def _clean(text: str) -> str:
    text = _UNICODE_JUNK.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return re.sub(r"\n+", " ", text).strip()


def _infer_work_mode(text: str) -> str:
    t = (text or "").lower()
    if "remote" in t:
        return "remote"
    if "hybrid" in t:
        return "hybrid"
    if "on-site" in t or "onsite" in t or "in office" in t:
        return "onsite"
    return "not_specified"


def _format_posted(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    if "T" in raw and len(raw) >= 10:
        return raw[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _delay(page: Page, lo: int, hi: int) -> None:
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


def _ensure_debug_dir() -> Path:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    return _DEBUG_DIR


async def _screenshot(page: Page, name: str) -> None:
    """Save a debug screenshot to debug/<name>.png (silently ignores errors)."""
    try:
        d = _ensure_debug_dir()
        await page.screenshot(path=str(d / f"{name}.png"), full_page=False)
        logger.debug("debug_screenshot_saved", name=name)
    except Exception as exc:
        logger.debug("debug_screenshot_failed", name=name, error=str(exc))


async def _save_html(page: Page, name: str) -> None:
    """Save full page HTML to debug/<name>.html (silently ignores errors)."""
    try:
        d = _ensure_debug_dir()
        content = await page.content()
        (d / f"{name}.html").write_text(content, encoding="utf-8")
        logger.debug("debug_html_saved", name=name)
    except Exception as exc:
        logger.debug("debug_html_failed", name=name, error=str(exc))


async def _retry(coro_fn, retries: int = 3, delay_s: float = 2.0):
    """Retry an async callable up to `retries` times on any exception."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.debug("retry_attempt", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay_s)
    raise last_exc  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInAgent:
    """
    LinkedIn job harvester using a persistent Chrome profile session.

    No login automation — the user logs in once via POST /linkedin-setup-session
    and the Chrome profile directory persists the session for all future runs.

    Instantiate fresh for each run.  PersistentBrowserManager is created and
    destroyed inside harvest().
    """

    def __init__(self) -> None:
        pass

    # ── Public API ─────────────────────────────────────────────────────────────

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[LinkedInScrapedJob]:
        """
        Open LinkedIn Jobs with the persistent Chrome profile and harvest
        jobs matching filters.  Returns [] if the profile is not authenticated
        (user must call POST /linkedin-setup-session first).
        """
        from app.services.config_service import ConfigService
        chrome_profile = ConfigService().load().browser.chrome_profile

        logger.info(
            "linkedin_agent_started",
            keyword        = filters.keyword,
            location       = filters.location,
            job_type       = filters.job_type,
            work_mode      = filters.work_mode,
            max_jobs       = filters.max_jobs,
            chrome_profile = chrome_profile,
        )
        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = headless,
            slow_mo     = slow_mo,
        ) as pbm:
            page = await pbm.new_page()
            jobs = await self._run(page, filters)

        logger.info(
            "linkedin_harvest_completed",
            total    = len(jobs),
            keyword  = filters.keyword,
            location = filters.location,
        )
        return jobs

    # ── Internal flow ──────────────────────────────────────────────────────────

    async def _run(self, page: Page, f: FiltersConfig) -> list[LinkedInScrapedJob]:
        """Navigate directly to LinkedIn Jobs search — no login step."""
        search_url = self._build_search_url(f, start=0)
        logger.info("linkedin_navigating_to_jobs", url=search_url)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error("linkedin_navigation_failed", error=str(exc))
            return []

        await page.wait_for_timeout(3_000)

        # Check for redirect to login — profile session missing or expired
        current_url = page.url
        if any(p in current_url for p in _GATED_PATHS):
            logger.error(
                "linkedin_not_authenticated",
                url  = current_url,
                hint = "Chrome profile has no LinkedIn session. "
                       "Call POST /linkedin-setup-session to log in.",
            )
            return []

        logger.info("linkedin_session_active", url=current_url)
        await _screenshot(page, "linkedin_jobs_page")
        return await self._paginate_and_collect(page, f)

    async def _paginate_and_collect(
        self, page: Page, f: FiltersConfig
    ) -> list[LinkedInScrapedJob]:
        """
        Paginate through LinkedIn results using &start=0,25,50,…

        Stops when:
        • No cards found on a page (results exhausted)
        • Two consecutive pages yield zero new (non-duplicate) jobs
        • Safety cap: f.max_jobs (0 = unlimited, default 500)
        """
        all_jobs:    list[LinkedInScrapedJob] = []
        seen_urls:   set[str]                 = set()
        page_num:    int                      = 0
        batch_size:  int                      = 25
        safety_cap:  int                      = f.max_jobs if f.max_jobs > 0 else 5_000
        empty_pages: int                      = 0

        logger.info(
            "linkedin_search_started",
            keyword   = f.keyword,
            location  = f.location,
            job_type  = f.job_type,
            work_mode = f.work_mode,
            max_jobs  = f.max_jobs,
        )
        logger.info("linkedin_pagination_started", safety_cap=safety_cap)

        while len(all_jobs) < safety_cap:
            start      = page_num * batch_size
            search_url = self._build_search_url(f, start=start)
            logger.info("linkedin_page_start", page=page_num + 1, start=start, collected=len(all_jobs))

            # Navigate
            async def _goto(url: str = search_url) -> None:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            try:
                await _retry(_goto)
            except Exception as exc:
                if page_num == 0:
                    await _screenshot(page, "linkedin_error")
                    await _save_html(page, "linkedin_error")
                    raise LinkedInLoginError(f"LinkedIn navigation failed: {exc}") from exc
                logger.warning("linkedin_page_nav_failed", page=page_num + 1, error=str(exc))
                break

            await _delay(page, 2_000, 3_000)
            self._check_blocked(page.url)
            await self._dismiss_overlays(page)

            if page_num == 0:
                await _screenshot(page, "linkedin_jobs_page")

            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                await _delay(page, 2_000, 3_000)

            self._check_blocked(page.url)
            await self._dismiss_overlays(page)

            if page_num == 0:
                logger.info("linkedin_jobs_page_opened", url=page.url)
                logger.info("linkedin_results_page_loaded", url=page.url)

            await self._scroll_results(page)

            if page_num == 0:
                await _screenshot(page, "linkedin_search_results")

            remaining = safety_cap - len(all_jobs)
            page_jobs = await self._extract_cards(page, remaining, seen_urls)

            if not page_jobs:
                empty_pages += 1
                logger.info("linkedin_page_empty", page=page_num + 1, consecutive_empty=empty_pages)
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
                for j in page_jobs:
                    url = (j.job_url or "").split("?")[0].rstrip("/").lower()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(j)

            logger.info("linkedin_page_done", page=page_num + 1, page_new=len(page_jobs), total=len(all_jobs))
            logger.info("linkedin_page_processed", page=page_num + 1, jobs_this_page=len(page_jobs), total_collected=len(all_jobs))
            page_num += 1
            await _delay(page, 1_500, 2_500)   # polite inter-page delay

        logger.info("linkedin_pagination_complete", pages=page_num, total=len(all_jobs))
        return all_jobs



    # ── Search URL builder ─────────────────────────────────────────────────────

    @staticmethod
    def _build_search_url(f: FiltersConfig, start: int = 0) -> str:
        params: list[str] = [
            f"keywords={quote_plus(f.keyword)}",
            f"location={quote_plus(f.location)}",
            "sortBy=DD",
        ]
        if wt := _WORK_MODE_MAP.get(f.work_mode, ""):
            params.append(f"f_WT={wt}")
        if jt := _JOB_TYPE_MAP.get(f.job_type, ""):
            params.append(f"f_JT={jt}")
        if tpr := _DATE_MAP.get(f.search_window_hours, ""):
            params.append(f"f_TPR={tpr}")
        if start > 0:
            params.append(f"start={start}")
        return _LINKEDIN_SEARCH_URL + "&".join(params)

    # ── Block detection ────────────────────────────────────────────────────────

    @staticmethod
    def _check_blocked(url: str) -> None:
        for pat in _GATED_PATHS:
            if pat in url:
                raise LinkedInLoginError(f"LinkedIn redirected to a gated page: {url}")

    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _Sel.COOKIE:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_200):
                    await el.click()
                    await _delay(page, 400, 700)
                    break
            except Exception:
                continue
        for sel in _Sel.MODAL_DISMISS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_000):
                    await el.click()
                    await _delay(page, 400, 700)
                    break
            except Exception:
                continue
        try:
            if await page.locator('div[role="dialog"]').first.is_visible(timeout=600):
                await page.keyboard.press("Escape")
                await _delay(page, 300, 600)
        except Exception:
            pass

    # ── Scrolling ──────────────────────────────────────────────────────────────

    async def _scroll_results(self, page: Page) -> None:
        container = None
        for sel in _Sel.CONTAINER:
            try:
                el = await page.query_selector(sel)
                if el:
                    container = el
                    logger.debug("linkedin_container_found", selector=sel)
                    break
            except Exception:
                continue

        if container:
            prev_h = -1
            for _ in range(25):
                h = await container.evaluate("el => el.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                await _delay(page, 400, 800)
        else:
            logger.debug("linkedin_no_container_scrolling_window")
            prev_h = -1
            for _ in range(15):
                h = await page.evaluate("document.body.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _delay(page, 600, 1_000)

    # ── Card extraction ────────────────────────────────────────────────────────

    async def _extract_cards(
        self,
        page:      Page,
        remaining: int,
        seen_urls: set[str] | None = None,
    ) -> list[LinkedInScrapedJob]:
        """Extract job cards from the current page. Returns only new (non-duplicate) jobs."""
        if seen_urls is None:
            seen_urls = set()

        for sel in _Sel.CARD:
            try:
                await page.wait_for_selector(sel, timeout=10_000)
                logger.debug("linkedin_card_selector_matched", selector=sel)
                break
            except Exception:
                continue

        raw: list[ElementHandle] = []
        for sel in _Sel.CARD:
            raw = await page.query_selector_all(sel)
            if raw:
                logger.info("linkedin_cards_found", selector=sel, count=len(raw))
                logger.info("linkedin_jobs_found", count=len(raw))
                break

        if not raw:
            await _save_html(page, "linkedin_no_cards")
            logger.warning("linkedin_no_cards_found")
            return []

        return await self._parse_cards_with_detail(page, raw, remaining, seen_urls)

    async def _parse_cards_with_detail(
        self,
        page:      Page,
        raw:       list[ElementHandle],
        remaining: int,
        seen_urls: set[str],
    ) -> list[LinkedInScrapedJob]:
        jobs: list[LinkedInScrapedJob] = []

        for idx, card_el in enumerate(raw):
            if len(jobs) >= remaining:
                break

            # ── Quick list-view pass (title, company, location, posted, url) ──
            list_data = await self._parse_card_list_view(card_el)
            if not list_data or not list_data.get("url"):
                continue
            url = list_data["url"]
            norm_url = url.split("?")[0].rstrip("/").lower()
            if norm_url and norm_url in seen_urls:
                continue

            # ── Click card to open detail panel ───────────────────────────────
            detail_data = await self._open_detail_panel(page, card_el, idx)

            # ── Merge list + detail data ──────────────────────────────────────
            title    = detail_data.get("title") or list_data.get("title") or ""
            company  = detail_data.get("company") or list_data.get("company") or "Unknown Company"
            location = detail_data.get("location") or list_data.get("location") or "Unknown Location"

            if not title:
                continue

            work_mode = _infer_work_mode(
                detail_data.get("emp_type", "") + " " + location
            )

            jobs.append(LinkedInScrapedJob(
                job_title       = _clean(title),
                company         = _clean(company),
                location        = _clean(location),
                salary          = _clean(detail_data.get("salary", "")) or "Not Disclosed",
                experience      = "Not Specified",
                posted_date     = _format_posted(
                    detail_data.get("posted") or list_data.get("posted") or ""
                ),
                job_url         = url,
                job_description = detail_data.get("description", ""),
                skills          = detail_data.get("skills", []),
                work_mode       = work_mode,
                company_url     = detail_data.get("company_url", ""),
                employment_type = _clean(detail_data.get("emp_type", "")),
                source          = "LinkedIn",
            ))
            logger.debug("linkedin_job_parsed", index=idx, title=title, company=company)

        logger.info("linkedin_extraction_complete", count=len(jobs))
        return jobs

    async def _parse_card_list_view(self, el: ElementHandle) -> dict | None:
        """Extract the minimal fields visible in the list-view card."""
        try:
            title = _clean(await _first_text(el, _Sel.TITLE))
            if not title:
                return None
            href      = await _first_attr(el, _Sel.LINK, "href")
            clean_url = href.split("?")[0] if href else ""
            return {
                "title":    title,
                "company":  _clean(await _first_text(el, _Sel.COMPANY)),
                "location": _clean(await _first_text(el, _Sel.LOCATION)),
                "posted":   (
                    await _first_attr(el, _Sel.POSTED, "datetime")
                    or await _first_text(el, _Sel.POSTED)
                ),
                "url": clean_url,
            }
        except Exception as exc:
            logger.debug("linkedin_list_view_parse_error", error=str(exc))
            return None

    async def _open_detail_panel(self, page: Page, card_el: ElementHandle, idx: int) -> dict:
        """Click a card and extract data from the right-side detail panel."""
        detail: dict = {}
        try:
            await card_el.scroll_into_view_if_needed()
            await _delay(page, 300, 600)
            await card_el.click(force=True)
            await _delay(page, 1_500, 2_500)

            # Wait for detail panel
            panel_found = False
            for sel in _Sel.DETAIL_PANEL:
                try:
                    await page.wait_for_selector(sel, timeout=8_000)
                    panel_found = True
                    logger.debug("linkedin_detail_panel_found", selector=sel, idx=idx)
                    break
                except Exception:
                    continue

            if not panel_found:
                logger.debug("linkedin_detail_panel_not_found", idx=idx)
                return detail

            detail["title"]       = await _first_text(page, _Sel.DETAIL_TITLE)
            detail["company"]     = await _first_text(page, _Sel.DETAIL_COMPANY)
            detail["company_url"] = await _first_attr(page, _Sel.DETAIL_COMPANY_URL, "href")
            detail["location"]    = await _first_text(page, _Sel.DETAIL_LOCATION)
            detail["posted"]      = (
                await _first_attr(page, _Sel.DETAIL_POSTED, "datetime")
                or await _first_text(page, _Sel.DETAIL_POSTED)
            )
            detail["emp_type"]    = await _first_text(page, _Sel.DETAIL_EMP_TYPE)
            detail["salary"]      = await _first_text(page, _Sel.DETAIL_SALARY)

            # Description — inner text of the whole panel
            for sel in _Sel.DETAIL_DESC:
                try:
                    desc_el = await page.query_selector(sel)
                    if desc_el:
                        raw_desc = await desc_el.inner_text()
                        if raw_desc and raw_desc.strip():
                            detail["description"] = raw_desc.strip()[:5000]
                            break
                except Exception:
                    continue

            # Skills list
            skills: list[str] = []
            for sel in _Sel.DETAIL_SKILLS:
                try:
                    skill_els = await page.query_selector_all(sel)
                    for s in skill_els:
                        t = await s.inner_text()
                        if t and t.strip():
                            skills.append(t.strip())
                    if skills:
                        break
                except Exception:
                    continue
            detail["skills"] = skills[:20]

        except Exception as exc:
            logger.debug("linkedin_detail_panel_error", idx=idx, error=str(exc))

        return detail
