"""
Dice.com scraper — browser automation for public Dice job search.
No login required.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import structlog
from playwright.async_api import ElementHandle, Page

from app.models.harvest_models import FiltersConfig

logger = structlog.get_logger(__name__)

_DICE_SEARCH_URL = "https://www.dice.com/jobs"
_DEBUG_DIR       = Path("debug")

_DATE_MAP: dict[int, str] = {
    24:  "ONE",
    48:  "THREE",
    72:  "THREE",
    168: "SEVEN",
    720: "THIRTY",
}
_JOB_TYPE_MAP: dict[str, str] = {
    "Contract":  "CONTRACTS",
    "Permanent": "FULLTIME",
    "Full-time": "FULLTIME",
    "Part-time": "PARTTIME",
    "Freelance": "CONTRACTS",
    "Any":       "",
}
_WORK_MODE_MAP: dict[str, str] = {
    "Remote": "Remote",
    "Hybrid": "Hybrid",
    "Onsite": "OnSite",
    "Any":    "",
}


@dataclass
class DiceScrapedJob:
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
    employment_type: str       = ""
    source:          str       = "Dice"


class _Sel:
    CARD: list[str] = [
        "dhi-search-card",
        "[data-cy='search-result']",
        "li[class*='search-result']",
        "[data-testid='job-card']",
        "article[class*='card']",
        "div[data-id]",
    ]
    TITLE: list[str] = [
        "a[data-cy='title-link']",
        "a[id$='-title']",
        "a[href*='/job-detail/']",
        "h5 a",
        "h4 a",
        "[data-testid='title'] a",
    ]
    COMPANY: list[str] = [
        "a.company-header-link",
        "a[data-cy='company']",
        "[data-testid='company-name']",
        "span[class*='company']",
        "a[href*='/employer/']",
        "[class*='company-link']",
    ]
    LOCATION: list[str] = [
        "span.search-result-location",
        "[data-testid='location']",
        "p[class*='location']",
        "span[class*='location']",
        "li[class*='location']",
    ]
    POSTED: list[str] = [
        "span.posted-date",
        "[data-testid='date']",
        "span[class*='posted']",
        "time",
        "relative-time",
    ]
    EMP_TYPE: list[str] = [
        "li[data-testid='employment-type']",
        "span[data-testid='employmentType']",
        "span[class*='employment']",
        "button[class*='chip'][class*='employ']",
    ]
    WORK_TYPE: list[str] = [
        "li[data-testid='workplace-type']",
        "span[data-testid='workplaceType']",
        "span[class*='workplace']",
        "span[class*='remote']",
        "button[class*='chip'][class*='work']",
    ]
    DESC: list[str] = [
        "div.job-search-preview p",
        "[data-testid='job-description']",
        "div[class*='description'] p",
        "span[class*='description']",
        "p[class*='snippet']",
    ]
    SKILLS: list[str] = [
        "button.skill-chip",
        "span.skill-chip",
        "li.skill-chip",
        "[class*='skill-chip']",
        "[class*='skill'] span",
    ]
    LINK: list[str] = [
        "a[data-cy='title-link']",
        "a[id$='-title']",
        "a[href*='/job-detail/']",
    ]
    CONTAINER: list[str] = [
        "div.serp-cards-container",
        "ul[class*='search-result']",
        "div[class*='search-results']",
        "#search-results",
        "main",
    ]
    NEXT_PAGE: list[str] = [
        "button[aria-label='Next']",
        "a[aria-label='Next']",
        "li.pagination-next a",
        "[data-testid='next-page']",
        "button[data-testid='pagination-next']",
    ]
    COOKIE: list[str] = [
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('I Accept')",
    ]
    MODAL: list[str] = [
        "button[aria-label='Close']",
        "button.modal-close",
        "button:has-text('Close')",
        "div[role='dialog'] button[aria-label='close']",
    ]


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
    if "on-site" in t or "onsite" in t or "on site" in t:
        return "onsite"
    return "not_specified"


def _format_posted(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = raw.strip().lower()
    today = datetime.now(timezone.utc)
    if not r or "today" in r or "just now" in r or "hour" in r or "minute" in r:
        return today.strftime("%Y-%m-%d")
    if "yesterday" in r:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*day", r)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    if re.match(r"\d{4}-\d{2}-\d{2}", r):
        return r[:10]
    return today.strftime("%Y-%m-%d")


async def _delay(page: Page, lo: int, hi: int) -> None:
    await page.wait_for_timeout(random.randint(lo, hi))


async def _first_text(root: "Page | ElementHandle", selectors: list[str]) -> str:
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


async def _first_attr(root: "Page | ElementHandle", selectors: list[str], attr: str) -> str:
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


async def _all_texts(root: "Page | ElementHandle", selectors: list[str]) -> list[str]:
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


def _ensure_debug_dir() -> Path:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    return _DEBUG_DIR


async def _screenshot(page: Page, name: str) -> None:
    try:
        d = _ensure_debug_dir()
        await page.screenshot(path=str(d / f"{name}.png"), full_page=False)
        logger.debug("debug_screenshot_saved", name=name)
    except Exception as exc:
        logger.debug("debug_screenshot_failed", name=name, error=str(exc))


async def _save_html(page: Page, name: str) -> None:
    try:
        d = _ensure_debug_dir()
        content = await page.content()
        (d / f"{name}.html").write_text(content, encoding="utf-8")
        logger.debug("debug_html_saved", name=name)
    except Exception as exc:
        logger.debug("debug_html_failed", name=name, error=str(exc))


class DiceScraper:
    """Low-level Dice.com browser scraper. Owned by DiceAgent."""

    def __init__(self, page: Page, filters: FiltersConfig) -> None:
        self._page    = page
        self._filters = filters

    # ── Public interface ───────────────────────────────────────────────────────

    async def login(self) -> None:
        """Dice.com is a public job board — no authentication required."""
        logger.info("dice_login_skipped", reason="public job board — no credentials needed")

    async def search_jobs(self, page_num: int = 1) -> None:
        """Navigate to the Dice search results URL for the given page."""
        url = self._build_search_url(self._filters, page_num)
        logger.info("dice_page_start", page=page_num, url=url)
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            if page_num == 1:
                await _screenshot(self._page, "dice_error")
                await _save_html(self._page, "dice_error")
            raise RuntimeError(f"Dice navigation failed on page {page_num}: {exc}") from exc
        await _delay(self._page, 800, 1_200)
        await self.apply_filters()

    async def apply_filters(self) -> None:
        """Dismiss cookie/modal overlays and wait for page to settle."""
        await self._dismiss_overlays()
        try:
            await self._page.wait_for_load_state("load", timeout=8_000)
        except Exception:
            await _delay(self._page, 500, 800)
        await self._dismiss_overlays()

    async def extract_job_cards(
        self,
        remaining: int,
        seen_urls: set[str],
    ) -> list[DiceScrapedJob]:
        """Extract job cards from the current page. Returns only new (non-duplicate) jobs."""
        await self._scroll_results()

        for sel in _Sel.CARD:
            try:
                await self._page.wait_for_selector(sel, timeout=8_000)
                logger.debug("dice_card_selector_matched", selector=sel)
                break
            except Exception:
                continue

        raw: list[ElementHandle] = []
        for sel in _Sel.CARD:
            raw = await self._page.query_selector_all(sel)
            if raw:
                logger.info("dice_cards_found", selector=sel, count=len(raw))
                break

        if not raw:
            await _save_html(self._page, "dice_no_cards")
            logger.warning("dice_no_cards_found_falling_back_to_js")
            return await self._js_extract_jobs(remaining, seen_urls)

        return await self._parse_card_elements(raw, remaining, seen_urls)

    async def paginate(self, current_page: int) -> bool:
        """
        Return True if a next page exists (i.e. next button is enabled).
        Return False if we are on the last page.
        Falls back to True (caller relies on empty-results counter).
        """
        for sel in _Sel.NEXT_PAGE:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    disabled = (
                        await el.get_attribute("aria-disabled")
                        or await el.get_attribute("disabled")
                    )
                    if disabled in ("true", "1"):
                        logger.info("dice_next_page_disabled", page=current_page)
                        return False
                    return True
            except Exception:
                continue
        return True  # no button found — rely on empty-pages counter

    def normalize(self, raw: dict) -> DiceScrapedJob:
        """Convert a raw card dict to a DiceScrapedJob."""
        url = raw.get("url", "")
        if url and not url.startswith("http"):
            url = f"https://www.dice.com{url}"
        return DiceScrapedJob(
            job_title       = _clean(raw.get("title", "")),
            company         = _clean(raw.get("company", "")) or "Unknown Company",
            location        = _clean(raw.get("location", "")) or "Not Specified",
            salary          = _clean(raw.get("salary", ""))  or "Not Disclosed",
            experience      = "Not Specified",
            posted_date     = _format_posted(raw.get("posted", "")),
            job_url         = url,
            job_description = _clean(raw.get("description", "")),
            skills          = raw.get("skills", [])[:20],
            work_mode       = _infer_work_mode(
                raw.get("work_type", "") + " " + raw.get("location", "")
            ),
            employment_type = _clean(raw.get("emp_type", "")),
            source          = "Dice",
        )

    async def run(self) -> list[DiceScrapedJob]:
        """
        Full harvest loop. Called by DiceAgent.

        Paginates via page=N URL parameter.
        Stops on: 2 consecutive empty pages, disabled Next button, or safety cap.
        """
        await self.login()

        all_jobs:    list[DiceScrapedJob] = []
        seen_urls:   set[str]             = set()
        page_num:    int                  = 1
        safety_cap:  int                  = self._filters.max_jobs if self._filters.max_jobs > 0 else 5_000
        empty_pages: int                  = 0

        while len(all_jobs) < safety_cap:
            try:
                await self.search_jobs(page_num)
            except RuntimeError as exc:
                if page_num == 1:
                    raise
                logger.warning("dice_page_nav_failed", page=page_num, error=str(exc))
                break

            if page_num == 1:
                await _screenshot(self._page, "dice_search_results")
                logger.info("dice_jobs_page_opened", url=self._page.url)

            remaining  = safety_cap - len(all_jobs)
            page_jobs  = await self.extract_job_cards(remaining, seen_urls)

            if not page_jobs:
                empty_pages += 1
                logger.info("dice_page_empty", page=page_num, consecutive_empty=empty_pages)
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
                for j in page_jobs:
                    url = (j.job_url or "").split("?")[0].rstrip("/").lower()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(j)

            logger.info("dice_page_done", page=page_num, page_new=len(page_jobs), total=len(all_jobs))

            if not await self.paginate(page_num):
                break

            page_num += 1
            await _delay(self._page, 400, 700)

        logger.info("dice_harvest_complete", pages=page_num, total=len(all_jobs))
        return all_jobs

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _build_search_url(f: FiltersConfig, page_num: int = 1) -> str:
        params: dict[str, str] = {}
        params["q"] = f.keyword if f.keyword else ""
        if f.location:
            params["location"] = f.location
        if date_val := _DATE_MAP.get(f.search_window_hours, ""):
            params["datePosted"] = date_val
        if emp_val := _JOB_TYPE_MAP.get(f.job_type, ""):
            params["employmentType"] = emp_val
        if work_val := _WORK_MODE_MAP.get(f.work_mode, ""):
            params["workplaceTypes"] = work_val
        if page_num > 1:
            params["page"] = str(page_num)
        qs = urlencode(params)
        return f"{_DICE_SEARCH_URL}?{qs}" if qs else _DICE_SEARCH_URL

    async def _dismiss_overlays(self) -> None:
        for sel in _Sel.COOKIE:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=400):
                    await el.click()
                    await _delay(self._page, 200, 350)
                    break
            except Exception:
                continue
        for sel in _Sel.MODAL:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=300):
                    await el.click()
                    await _delay(self._page, 200, 350)
                    break
            except Exception:
                continue
        try:
            if await self._page.locator('div[role="dialog"]').first.is_visible(timeout=300):
                await self._page.keyboard.press("Escape")
                await _delay(self._page, 200, 400)
        except Exception:
            pass

    async def _scroll_results(self) -> None:
        container = None
        for sel in _Sel.CONTAINER:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    container = el
                    logger.debug("dice_container_found", selector=sel)
                    break
            except Exception:
                continue

        if container:
            prev_h = -1
            for _ in range(6):
                h = await container.evaluate("el => el.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                await _delay(self._page, 200, 350)
        else:
            prev_h = -1
            for _ in range(4):
                h = await self._page.evaluate("document.body.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _delay(self._page, 300, 500)

    async def _parse_card_elements(
        self,
        raw:       list[ElementHandle],
        remaining: int,
        seen_urls: set[str],
    ) -> list[DiceScrapedJob]:
        jobs: list[DiceScrapedJob] = []
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

    async def _parse_card(self, el: ElementHandle) -> DiceScrapedJob | None:
        try:
            title = _clean(await _first_text(el, _Sel.TITLE))
            if not title:
                return None

            href     = await _first_attr(el, _Sel.LINK, "href")
            job_url  = href if href and href.startswith("http") else (
                f"https://www.dice.com{href}" if href else ""
            )

            company     = _clean(await _first_text(el, _Sel.COMPANY))  or "Unknown Company"
            location    = _clean(await _first_text(el, _Sel.LOCATION)) or "Not Specified"
            posted_raw  = _clean(await _first_text(el, _Sel.POSTED))
            emp_type    = _clean(await _first_text(el, _Sel.EMP_TYPE))
            work_type   = _clean(await _first_text(el, _Sel.WORK_TYPE))
            description = _clean(await _first_text(el, _Sel.DESC))
            skills      = [_clean(s) for s in await _all_texts(el, _Sel.SKILLS) if s.strip()]

            work_mode = _infer_work_mode(work_type + " " + location)

            return DiceScrapedJob(
                job_title       = title,
                company         = company,
                location        = location,
                salary          = "Not Disclosed",
                experience      = "Not Specified",
                posted_date     = _format_posted(posted_raw),
                job_url         = job_url,
                job_description = description,
                skills          = skills[:15],
                work_mode       = work_mode,
                employment_type = emp_type,
                source          = "Dice",
            )
        except Exception as exc:
            logger.debug("dice_card_parse_error", error=str(exc))
            return None

    async def _js_extract_jobs(
        self,
        remaining: int,
        seen_urls: set[str] | None = None,
    ) -> list[DiceScrapedJob]:
        """JavaScript fallback: find all /job-detail/ links and extract card data."""
        logger.info("dice_js_extraction_started")
        if seen_urls is None:
            seen_urls = set()
        try:
            raw_jobs: list[dict] = await self._page.evaluate("""
                () => {
                    function getText(el, sels) {
                        for (const s of sels) {
                            try {
                                const found = el.querySelector(s);
                                if (found && found.textContent.trim())
                                    return found.textContent.trim();
                            } catch(e) {}
                        }
                        return '';
                    }
                    function getAttr(el, sels, attr) {
                        for (const s of sels) {
                            try {
                                const found = el.querySelector(s);
                                if (found) {
                                    const v = found.getAttribute(attr);
                                    if (v) return v.trim();
                                }
                            } catch(e) {}
                        }
                        return '';
                    }

                    const anchors = Array.from(document.querySelectorAll('a[href*="/job-detail/"]'));
                    const seen = new Set();
                    const results = [];
                    for (const a of anchors) {
                        const rawUrl = a.href || '';
                        const url = rawUrl.split('?')[0];
                        if (!url || seen.has(url)) continue;
                        seen.add(url);

                        let card = a;
                        for (let i = 0; i < 8; i++) {
                            const p = card.parentElement;
                            if (!p) break;
                            card = p;
                            const tag = card.tagName;
                            const cls = card.className || '';
                            if (tag === 'ARTICLE' || tag === 'LI') break;
                            if (/card|result|tuple/i.test(cls)) break;
                            if (card.tagName.toLowerCase() === 'dhi-search-card') break;
                        }

                        const title = a.textContent.trim() ||
                            getText(card, ['h5 a', 'h4 a', '[data-testid="title"] a']);
                        if (!title || title.length < 3) continue;

                        results.push({
                            url,
                            title,
                            company:     getText(card, ['a.company-header-link','a[data-cy="company"]','[data-testid="company-name"]','span[class*="company"]']),
                            location:    getText(card, ['span.search-result-location','[data-testid="location"]','span[class*="location"]']),
                            posted:      getText(card, ['span.posted-date','[data-testid="date"]','span[class*="posted"]','time']),
                            emp_type:    getText(card, ['[data-testid="employmentType"]','span[class*="employment"]']),
                            work_type:   getText(card, ['[data-testid="workplaceType"]','span[class*="workplace"]','span[class*="remote"]']),
                            description: getText(card, ['div.job-search-preview p','span[class*="description"]','p[class*="snippet"]']),
                            skills:      Array.from(card.querySelectorAll('button.skill-chip, span.skill-chip, [class*="skill-chip"]'))
                                              .map(e => e.textContent.trim()).filter(Boolean),
                        });
                        if (results.length >= 50) break;
                    }
                    return results;
                }
            """)
        except Exception as exc:
            logger.error("dice_js_extraction_failed", error=str(exc))
            return []

        if not raw_jobs:
            logger.warning("dice_js_extraction_no_jobs")
            return []

        logger.info("dice_js_extraction_found", count=len(raw_jobs))
        jobs: list[DiceScrapedJob] = []
        for r in raw_jobs:
            if len(jobs) >= remaining:
                break
            url = r.get("url", "")
            if not url:
                continue
            norm = url.split("?")[0].rstrip("/").lower()
            if norm and norm in seen_urls:
                continue
            jobs.append(self.normalize(r))
        return jobs
