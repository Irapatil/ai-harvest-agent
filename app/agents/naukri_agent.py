"""
Naukri.com Harvest Agent — autonomous job scraper.

Responsibilities
────────────────
1. Receive FiltersConfig (keyword, location, job_type, work_mode, salary, …)
2. Login to Naukri.com using credentials from Settings (NAUKRI_EMAIL / NAUKRI_PASSWORD)
3. Build the Naukri Jobs search URL with all filter parameters encoded
4. Open the page via BrowserManager (non-headless, stealth)
5. Dismiss overlays / login prompts
6. Scroll the result container until all virtual-DOM cards are loaded
7. Parse every visible job card via dynamic fallback selector chains
8. Return a deduplicated list[NaukriScrapedJob]

Filter URL parameters (Naukri)
──────────────────────────────
work_mode  Remote → wfhType=3  |  Hybrid → wfhType=1  |  Onsite → (omit)
job_type   Permanent → jobType=1  |  Contract → jobType=2  |  Part-time → jobType=5
date       24h → jobAge=1  |  7 days → jobAge=7  |  30 days → jobAge=30
salary     <min_lpa>,<max_lpa>  (INR only — auto-converted from absolute to LPA)
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import structlog
from playwright.async_api import ElementHandle, Page

from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import PersistentBrowserManager

logger = structlog.get_logger(__name__)

_DEBUG_DIR = Path("data/debug/naukri")


# ── Filter → URL param maps ───────────────────────────────────────────────────

_WORK_MODE_MAP: dict[str, str] = {
    "Remote":  "3",   # WFH
    "Hybrid":  "1",   # Hybrid
    "Onsite":  "",    # Work from office (omit filter)
    "Any":     "",
}
_JOB_TYPE_MAP: dict[str, str] = {
    "Permanent": "1",
    "Contract":  "2",
    "Part-time": "5",
    "Any":       "",
}
_DATE_MAP: dict[int, str] = {
    24:  "1",
    168: "7",
    720: "30",
}

# recruit.naukri.com → redirects to /recruit/login?msg=TO&URL=recruit.naukri.com
# Clicking the "Register/Log in" tab switches to the login form.
# NOTE: Naukri now requires "Naukri Launcher" app for recruiter login —
#       automated login will fail; agent falls back to guest/public search.
_NAUKRI_LOGIN_URL        = "https://recruit.naukri.com/"
_NAUKRI_LOGIN_URL_SEEKER = "https://www.naukri.com/nlogin/login"   # seeker/public fallback
_NAUKRI_SEARCH_URL       = "https://www.naukri.com/jobs-in-india"


# ── Scraped job dataclass ─────────────────────────────────────────────────────

@dataclass
class NaukriScrapedJob:
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    skills:          list[str] = field(default_factory=list)
    work_mode:       str = "not_specified"
    source:          str = "Naukri"


# ── CSS selector fallback chains ──────────────────────────────────────────────

class _Sel:
    # ── Login form ────────────────────────────────────────────────────────────
    LOGIN_EMAIL: list[str] = [
        "input#usernameField",
        "input[placeholder*='Email']",
        "input[placeholder*='Username']",
        "input[type='text'][name*='user']",
        "input[type='email']",
    ]
    LOGIN_PASSWORD: list[str] = [
        "input#passwordField",
        "input[placeholder*='assword']",
        "input[type='password']",
        "input[name*='pass']",
    ]
    LOGIN_SUBMIT: list[str] = [
        "button.loginButton",
        "button[type='submit']",
        "button:has-text('Login')",
        "input[type='submit']",
    ]
    LOGIN_ERROR: list[str] = [
        ".naukri-err-msg",
        ".error-msg",
        "[class*='errTxt']",
        "span[class*='err']",
        "div.alert",
    ]

    # ── Overlays ──────────────────────────────────────────────────────────────
    COOKIE: list[str] = [
        "button[class*='accept']",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('OK')",
    ]
    MODAL_DISMISS: list[str] = [
        "button[class*='close']",
        "span[class*='close-icon']",
        "button[aria-label='Close']",
        "button:has-text('Skip')",
        "button:has-text('Not now')",
        "button:has-text('Cancel')",
        ".modal-close",
    ]

    # ── Result container + individual cards ───────────────────────────────────
    CONTAINER: list[str] = [
        "#listContainer",
        ".list-container",
        ".srp-jobtuple-wrapper",
        "[class*='listContainer']",
        "[class*='resultContainer']",
        "[class*='list-container']",
        ".search-result-list",
        "main",
    ]
    # Naukri 2024-2026 uses article[data-job-id]; old site used .jobTuple
    CARD: list[str] = [
        "article[data-job-id]",
        "article.jobTuple",
        "article[class*='tuple']",
        "article[class*='job']",
        ".srp-jobtuple-wrapper article",
        ".jobTuple",
        "div[data-job-id]",
        "div[class*='jobTuple']",
        "div[class*='job-card']",
        "li[data-job-id]",
        "li[class*='jobTuple']",
    ]

    # ── Card fields ────────────────────────────────────────────────────────────
    TITLE: list[str] = [
        "a.title",
        "a[class*='title']",
        ".title a",
        "h2 a",
        "h3 a",
        "[class*='job-title'] a",
        "[class*='jobTitle'] a",
        "a[href*='/job-listings/']",
    ]
    COMPANY: list[str] = [
        "a.comp-name",
        "a.subTitle",
        ".comp-name",
        "[class*='comp-name']",
        "a[class*='comp']",
        "[class*='company'] a",
        "[class*='companyName'] a",
        "span[class*='company']",
    ]
    LOCATION: list[str] = [
        "span.locWdth",
        "li.location span.ellipsis",
        "span[class*='loc']",
        "[class*='location'] span",
        "li.location",
        "[class*='locWdth']",
        "span[class*='Location']",
    ]
    SALARY: list[str] = [
        "li.salary span.ellipsis",
        "span.salary-text",
        "span[class*='salary']",
        "li.salary",
        "[class*='sal'] span",
        "[class*='Salary'] span",
    ]
    EXPERIENCE: list[str] = [
        "li.experience span.ellipsis",
        "span[class*='exp']",
        "li.experience",
        "[class*='experience'] span",
        "[class*='Experience'] span",
        "span[class*='expwdth']",
    ]
    POSTED: list[str] = [
        "span.job-post-day",
        "span[class*='fresh']",
        "span[class*='time']",
        "span[class*='posted']",
        "span[class*='date']",
        "time",
    ]
    DESCRIPTION: list[str] = [
        "div.job-description",
        "[class*='job-description']",
        "[class*='jobDesc']",
        "span[class*='desc']",
        ".jd-desc",
        "[class*='snippet']",
    ]
    SKILLS: list[str] = [
        "ul.tags-gt li a",
        "ul[class*='tag'] li a",
        "li.tag-li a",
        "[class*='skills'] a",
        "[class*='skill'] span",
        "[class*='tags'] a",
        "[class*='Tags'] a",
    ]
    LINK: list[str] = [
        "a[href*='/job-listings/']",
        "a.title",
        "a[class*='title']",
        "a[href*='naukri.com/']",
        "h2 a",
        "h3 a",
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

_UNICODE_JUNK = re.compile(r"[ ​‌‍﻿­]")
_WHITESPACE   = re.compile(r"[ \t]{2,}")


def _clean(text: str) -> str:
    text = _UNICODE_JUNK.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return re.sub(r"\n+", " ", text).strip()


def _infer_work_mode(location: str) -> str:
    loc = (location or "").lower()
    if "remote" in loc or "work from home" in loc or "wfh" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    if "on-site" in loc or "onsite" in loc:
        return "onsite"
    return "not_specified"


def _format_posted(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
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


async def _all_texts(root: Page | ElementHandle, selectors: list[str]) -> list[str]:
    """Collect inner text from all matching elements (used for skills list)."""
    for sel in selectors:
        try:
            els = await root.query_selector_all(sel)
            if els:
                texts = [t for el in els if (t := (await el.inner_text() or "").strip())]
                if texts:
                    return texts
        except Exception:
            continue
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Naukri Agent
# ══════════════════════════════════════════════════════════════════════════════

class NaukriAgent:
    """
    Naukri.com job harvester using a persistent Chrome profile session.

    No login automation — the user logs in once via POST /naukri-setup-session
    and the Chrome profile directory persists the session for all future runs.

    Instantiate fresh for each run.
    """

    def __init__(self) -> None:
        pass

    # ── Public API ─────────────────────────────────────────────────────────────

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[NaukriScrapedJob]:
        """
        Open Naukri with the persistent Chrome profile and harvest jobs
        matching filters.  Returns [] if the profile is not authenticated.
        """
        from app.services.config_service import ConfigService
        chrome_profile = ConfigService().load().browser.chrome_profile

        logger.info(
            "config_loaded",
            source              = "naukri",
            keyword             = filters.keyword,
            location            = filters.location,
            job_type            = filters.job_type,
            work_mode           = filters.work_mode,
            domain              = getattr(filters, "domain", "Any"),
            search_window_hours = filters.search_window_hours,
            max_jobs            = filters.max_jobs,
            chrome_profile      = chrome_profile,
        )
        logger.info(
            "naukri_search_started",
            keyword        = filters.keyword,
            location       = filters.location,
            job_type       = filters.job_type,
            work_mode      = filters.work_mode,
            max_jobs       = filters.max_jobs,
            chrome_profile = chrome_profile,
        )
        try:
            async with PersistentBrowserManager(
                profile_dir = chrome_profile,
                headless    = headless,
                slow_mo     = slow_mo,
            ) as pbm:
                page = await pbm.new_page()
                jobs = await self._run(page, filters)
            logger.info("naukri_jobs_extracted", total=len(jobs))
            logger.info("agent_completed", source="naukri", total=len(jobs))
            return jobs
        except Exception as exc:
            logger.exception("agent_failed", source="naukri", error=str(exc))
            return []

    # ── Internal flow ──────────────────────────────────────────────────────────

    async def _run(self, page: Page, f: FiltersConfig) -> list[NaukriScrapedJob]:
        """Navigate directly to Naukri search — no login step."""
        return await self._paginate_and_collect(page, f)

    async def _paginate_and_collect(
        self, page: Page, f: FiltersConfig
    ) -> list[NaukriScrapedJob]:
        """
        Paginate through Naukri results using pageNo=1,2,3,…

        Stops when:
        • No cards found on a page (results exhausted)
        • Two consecutive empty pages
        • Safety cap: f.max_jobs (0 = unlimited, default 500)
        """
        all_jobs:    list[NaukriScrapedJob] = []
        seen_urls:   set[str]               = set()
        page_num:    int                    = 1   # Naukri pages are 1-indexed
        safety_cap:  int                    = f.max_jobs if f.max_jobs > 0 else 5_000
        empty_pages: int                    = 0

        logger.info(
            "search_started",
            source      = "naukri",
            keyword     = f.keyword,
            location    = f.location,
            job_type    = f.job_type,
            work_mode   = f.work_mode,
            max_jobs    = f.max_jobs,
            search_window_hours = f.search_window_hours,
        )
        logger.info("pagination_started", source="naukri", safety_cap=safety_cap)

        while len(all_jobs) < safety_cap:
            search_url = self._build_search_url(f, page_no=page_num)
            logger.info("search_url_generated", source="naukri", page=page_num, url=search_url)
            logger.info("naukri_page_start", page=page_num, collected=len(all_jobs))

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                if page_num == 1:
                    logger.error("naukri_navigation_failed", error=str(exc))
                    raise
                logger.warning("naukri_page_nav_failed", page=page_num, error=str(exc))
                break

            await _delay(page, 800, 1_200)
            self._check_blocked(page.url)
            await self._dismiss_overlays(page)

            page_title = await page.title()
            logger.info("search_page_opened", source="naukri", page=page_num, url=page.url, title=page_title)

            logger.info("waiting_for_results", source="naukri", page=page_num, url=page.url)
            try:
                await page.wait_for_load_state("load", timeout=8_000)
            except Exception:
                await _delay(page, 500, 800)

            page_title = await page.title()
            logger.info("results_page_loaded", source="naukri", page=page_num, url=page.url, title=page_title)
            self._check_blocked(page.url)
            await self._dismiss_overlays(page)

            await self._screenshot(page, f"naukri_page_{page_num:02d}")

            await self._scroll_results(page)

            remaining  = safety_cap - len(all_jobs)
            page_jobs  = await self._extract_cards(page, remaining, seen_urls)

            if not page_jobs:
                empty_pages += 1
                logger.info("naukri_page_empty", page=page_num, consecutive_empty=empty_pages)
                if empty_pages >= 2:
                    logger.info("next_page_not_found", source="naukri", page=page_num, reason="consecutive_empty_pages")
                    break
            else:
                empty_pages = 0
                for j in page_jobs:
                    url = (j.job_url or "").split("?")[0].rstrip("/").lower()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(j)
                logger.info("next_page_found", source="naukri", page=page_num, jobs_this_page=len(page_jobs))

            logger.info("naukri_page_done", page=page_num, page_new=len(page_jobs), total=len(all_jobs))
            logger.info("page_processed", source="naukri", page=page_num, jobs_this_page=len(page_jobs), total_collected=len(all_jobs))
            page_num += 1
            await _delay(page, 400, 700)

        logger.info("pagination_completed", source="naukri", pages=page_num - 1, total=len(all_jobs))
        logger.info("naukri_pagination_complete", pages=page_num - 1, total=len(all_jobs))
        logger.info("jobs_found", source="naukri", total=len(all_jobs))
        return all_jobs


    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _Sel.COOKIE:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=400):
                    await el.click()
                    await _delay(page, 200, 350)
                    break
            except Exception:
                continue

        for sel in _Sel.MODAL_DISMISS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=300):
                    await el.click()
                    await _delay(page, 200, 350)
                    break
            except Exception:
                continue

        try:
            if await page.locator('div[role="dialog"]').first.is_visible(timeout=300):
                await page.keyboard.press("Escape")
                await _delay(page, 200, 400)
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
                    logger.info("jobs_container_found", source="naukri", selector=sel)
                    break
            except Exception:
                continue

        if not container:
            logger.info("jobs_container_not_found", source="naukri", selectors_tried=_Sel.CONTAINER)

        if container:
            prev_h = -1
            for _ in range(6):
                h = await container.evaluate("el => el.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                await _delay(page, 200, 350)
        else:
            logger.debug("naukri_no_container_scrolling_window")
            prev_h = -1
            for _ in range(4):
                h = await page.evaluate("document.body.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _delay(page, 300, 500)

    # ── Card extraction ────────────────────────────────────────────────────────

    async def _extract_cards(
        self,
        page:      Page,
        remaining: int,
        seen_urls: set[str] | None = None,
    ) -> list[NaukriScrapedJob]:
        """Extract job cards from the current page. Returns only new (non-duplicate) jobs."""
        if seen_urls is None:
            seen_urls = set()

        for sel in _Sel.CARD:
            try:
                await page.wait_for_selector(sel, timeout=5_000)
                logger.info("naukri_card_selector_matched", selector=sel)
                break
            except Exception:
                logger.info("naukri_selector_timeout", selector=sel)
                continue

        raw: list[ElementHandle] = []
        for sel in _Sel.CARD:
            found = await page.query_selector_all(sel)
            cnt   = len(found)
            logger.info("naukri_selector_tried", selector=sel, count=cnt)
            if found:
                raw = found
                logger.info("job_cards_found_count", source="naukri", selector=sel, count=cnt)
                logger.info("naukri_cards_found", selector=sel, count=cnt)
                break

        if not raw:
            await self._screenshot(page, "naukri_no_cards")
            logger.warning("naukri_job_cards_not_found", source="naukri", selectors_tried=_Sel.CARD)
            logger.warning("naukri_no_cards_found_css_selectors_falling_back_to_js")
            return await self._js_extract_jobs(page, remaining, seen_urls)

        return await self._parse_card_elements(raw, remaining, seen_urls)

    async def _js_extract_jobs(
        self, page: Page, remaining: int, seen_urls: set[str] | None = None
    ) -> list[NaukriScrapedJob]:
        """JavaScript-based extraction: find all Naukri job links then walk up to card containers."""
        logger.info("naukri_js_extraction_started")
        try:
            raw_jobs: list[dict] = await page.evaluate("""
                () => {
                    function getText(el, sels) {
                        for (const s of sels) {
                            try {
                                const found = el.querySelector(s);
                                if (found && found.textContent.trim()) return found.textContent.trim();
                            } catch(e) {}
                        }
                        return '';
                    }

                    // Find anchors that look like Naukri job-detail links
                    const allAnchors = Array.from(document.querySelectorAll('a[href]'));
                    const jobAnchors = allAnchors.filter(a => {
                        const h = a.href || '';
                        return h.includes('naukri.com') && (
                            h.includes('/job-listings/') ||
                            /\\/[a-z][a-z0-9-]+-\\d{7,}(\\?|$)/i.test(h)
                        );
                    });

                    const seen = new Set();
                    const results = [];
                    for (const a of jobAnchors) {
                        const url = a.href.split('?')[0];
                        if (seen.has(url)) continue;
                        seen.add(url);

                        // Walk up to find the card container (article, li, or a named div)
                        let card = a;
                        for (let i = 0; i < 8; i++) {
                            const p = card.parentElement;
                            if (!p) break;
                            card = p;
                            const tag = card.tagName;
                            const cls = card.className || '';
                            if (tag === 'ARTICLE' || tag === 'LI') break;
                            if (/job|tuple|card/i.test(cls)) break;
                        }

                        const titleSels = [
                            'a.title', 'a[class*="title"]', '.title a', 'h2 a', 'h3 a',
                            '[class*="job-title"] a', '[class*="jobTitle"] a'
                        ];
                        const title = a.textContent.trim() || getText(card, titleSels);
                        if (!title || title.length < 3) continue;

                        results.push({
                            url,
                            title,
                            company: getText(card, ['a.comp-name','a.subTitle','.comp-name','[class*="comp-name"]','[class*="company"] a','[class*="companyName"]']),
                            location: getText(card, ['span.locWdth','[class*="loc"]','[class*="location"] span','li.location']),
                            salary: getText(card, ['li.salary','.salary','[class*="salary"]','[class*="Salary"]']),
                            experience: getText(card, ['li.experience','[class*="exp"]','[class*="expwdth"]']),
                            posted: getText(card, ['span.job-post-day','[class*="fresh"]','[class*="posted"]','time']),
                            description: getText(card, ['[class*="job-description"]','[class*="desc"]','[class*="snippet"]']),
                        });
                        if (results.length >= 50) break;
                    }
                    return results;
                }
            """)
        except Exception as exc:
            logger.error("naukri_js_extraction_failed", error=str(exc))
            return []

        if not raw_jobs:
            logger.warning("naukri_js_extraction_no_jobs")
            return []

        logger.info("naukri_js_extraction_found", count=len(raw_jobs))
        if seen_urls is None:
            seen_urls = set()
        jobs: list[NaukriScrapedJob] = []
        for j in raw_jobs:
            if len(jobs) >= remaining:
                break
            url = j.get("url", "")
            if not url:
                continue
            norm = url.split("?")[0].rstrip("/").lower()
            if norm and norm in seen_urls:
                continue
            if not url.startswith("http"):
                url = f"https://www.naukri.com{url}"
            jobs.append(NaukriScrapedJob(
                job_title       = j.get("title", ""),
                company         = j.get("company", "") or "Unknown Company",
                location        = j.get("location", "") or "Unknown Location",
                salary          = j.get("salary", "") or "Not Disclosed",
                experience      = j.get("experience", "") or "Not Specified",
                posted_date     = _format_posted(j.get("posted", "")),
                job_url         = url,
                job_description = j.get("description", ""),
                skills          = [],
                work_mode       = _infer_work_mode(j.get("location", "")),
                source          = "Naukri",
            ))
        return jobs

    async def _parse_card_elements(
        self,
        raw:       list[ElementHandle],
        remaining: int,
        seen_urls: set[str],
    ) -> list[NaukriScrapedJob]:
        jobs: list[NaukriScrapedJob] = []
        for el in raw:
            if len(jobs) >= remaining:
                break
            job = await self._parse_card(el)
            if not job or not job.job_url:
                continue
            norm = job.job_url.split("?")[0].rstrip("/").lower()
            if norm and norm in seen_urls:
                continue
            jobs.append(job)
        return jobs

    async def _parse_card(self, el: ElementHandle) -> NaukriScrapedJob | None:
        try:
            title = _clean(await _first_text(el, _Sel.TITLE))
            if not title:
                return None

            company     = _clean(await _first_text(el, _Sel.COMPANY))    or "Unknown Company"
            location    = _clean(await _first_text(el, _Sel.LOCATION))   or "Unknown Location"
            salary      = _clean(await _first_text(el, _Sel.SALARY))     or "Not Disclosed"
            experience  = _clean(await _first_text(el, _Sel.EXPERIENCE)) or "Not Specified"
            posted_raw  = _clean(await _first_text(el, _Sel.POSTED))
            description = _clean(await _first_text(el, _Sel.DESCRIPTION))
            skills      = await _all_texts(el, _Sel.SKILLS)

            # Resolve job URL
            href = (
                await _first_attr(el, _Sel.LINK, "href")
                or await _first_attr(el, _Sel.TITLE, "href")
            )
            clean_url = href.split("?")[0] if href and "?" in href else (href or "")
            if clean_url and not clean_url.startswith("http"):
                clean_url = f"https://www.naukri.com{clean_url}"

            job = NaukriScrapedJob(
                job_title       = title,
                company         = company,
                location        = location,
                salary          = salary,
                experience      = experience,
                posted_date     = _format_posted(posted_raw),
                job_url         = clean_url,
                job_description = description,
                skills          = skills[:15],
                work_mode       = _infer_work_mode(location),
                source          = "Naukri",
            )
            logger.info("job_card_extracted", source="naukri", title=title, company=company, url=clean_url)
            return job
        except Exception as exc:
            logger.debug("naukri_card_parse_error", error=str(exc))
            return None

    # ── Search URL builder ─────────────────────────────────────────────────────

    @staticmethod
    def _build_search_url(f: FiltersConfig, page_no: int = 1) -> str:
        """Encode all active filter params into the Naukri search URL."""
        params: dict[str, str] = {
            "k": f.keyword,
            "l": f.location,
        }
        if tpr := _DATE_MAP.get(f.search_window_hours, ""):
            params["jobAge"] = tpr
        if wt := _WORK_MODE_MAP.get(f.work_mode, ""):
            params["wfhType"] = wt
        if jt := _JOB_TYPE_MAP.get(f.job_type, ""):
            params["jobType"] = jt
        if f.salary_currency == "INR" and f.salary_min:
            lpa_min = max(1, int(f.salary_min // 100_000))
            if f.salary_max:
                lpa_max = max(lpa_min + 1, int(f.salary_max // 100_000))
                params["salary"] = f"{lpa_min},{lpa_max}"
            else:
                params["salary"] = str(lpa_min)
        if page_no > 1:
            params["pageNo"] = str(page_no)

        return f"{_NAUKRI_SEARCH_URL}?{urlencode(params)}"

    # ── Block detection ────────────────────────────────────────────────────────

    @staticmethod
    def _check_blocked(url: str) -> None:
        # Only block mid-search-flow redirects; login URLs are handled inside _login()
        for pat in ("/challenge", "/authwall", "/nlogin/login"):
            if pat in url:
                raise RuntimeError(f"Naukri redirected to a gated page: {url}")

    # ── Screenshot helper ──────────────────────────────────────────────────────

    async def _screenshot(self, page: Page, label: str) -> None:
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = _DEBUG_DIR / f"{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=False)
            logger.info("naukri_screenshot_saved", path=str(path))
        except Exception as exc:
            logger.debug("naukri_screenshot_failed", error=str(exc))
