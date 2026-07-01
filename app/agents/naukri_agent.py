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

_DEBUG_DIR         = Path("data/debug/naukri")
_DEBUG_SCREENSHOT  = Path("data/debug/screenshots")
_DEBUG_HTML        = Path("data/debug/html")


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
    48:  "2",
    72:  "3",
    168: "7",
    336: "14",
    720: "30",
}

# recruit.naukri.com → redirects to /recruit/login?msg=TO&URL=recruit.naukri.com
# Clicking the "Register/Log in" tab switches to the login form.
# NOTE: Naukri now requires "Naukri Launcher" app for recruiter login —
#       automated login will fail; agent falls back to guest/public search.
_NAUKRI_LOGIN_URL        = "https://recruit.naukri.com/"
_NAUKRI_LOGIN_URL_SEEKER = "https://www.naukri.com/nlogin/login"   # seeker/public fallback
_NAUKRI_SEARCH_URL       = "https://www.naukri.com/jobs-in-india"


class SearchFilterNotAppliedException(RuntimeError):
    """
    Raised when Naukri's jobAge (search_window_hours) filter is not present
    in the final URL after navigation and one retry.  Harvesting is aborted
    to prevent collecting all-time results instead of the intended window.
    """


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
    # Lead intelligence
    recruiter_name:       str | None = None
    recruiter_company:    str | None = None
    job_poster_designation: str | None = None
    email_id:             str | None = None
    contact_number:       str | None = None


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
        "div[data-job-id]",          # current Naukri 2025 DOM
        "article[data-job-id]",      # legacy
        "li[data-job-id]",
        "article.jobTuple",
        "article[class*='tuple']",
        "article[class*='job']",
        ".srp-jobtuple-wrapper article",
        ".jobTuple",
        "div[class*='jobTuple']",
        "div[class*='job-card']",
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
    # Recruiter / HR contact info (visible on some Naukri cards)
    RECRUITER_NAME: list[str] = [
        "a.nameHd",
        ".rcName",
        "[class*='rcName']",
        "[class*='recruiterName']",
        "[data-automation='recruiter-name']",
        "span[class*='recName']",
    ]
    RECRUITER_COMPANY: list[str] = [
        ".rcCompanyName",
        "[class*='rcCompany']",
        "[class*='recruiterCompany']",
        "span[class*='compHd']",
    ]
    RECRUITER_EMAIL: list[str] = [
        ".rcEmail a",
        "[class*='rcEmail']",
        "a[href^='mailto:']",
    ]
    RECRUITER_PHONE: list[str] = [
        ".phoneNum",
        "[class*='phone']",
        "[class*='mobile']",
        "a[href^='tel:']",
    ]

    # ── Detail page — recruiter / HR contact (navigate to job URL) ────────────
    DETAIL_RECRUITER_NAME: list[str] = [
        "a.nameHd",
        ".nameHd",
        ".rcName",
        "[class*='rcName']",
        "[class*='recruiterName']",
        "span[class*='recName']",
        ".jd-dInfoSec a",
        "[class*='hirerName']",
    ]
    DETAIL_RECRUITER_DESIGNATION: list[str] = [
        ".rcDesig",
        "span.rcDesig",
        "[class*='rcDesig']",
        "[class*='designation']",
        "p.rcDesig",
        "[class*='Desig']",
        ".ppw-desg",
    ]
    DETAIL_RECRUITER_COMPANY: list[str] = [
        ".rcCompanyName",
        "[class*='rcCompany']",
        "[class*='recruiterCompany']",
        "span[class*='compHd']",
    ]
    DETAIL_EMAIL: list[str] = [
        "a[href^='mailto:']",
        ".rcEmail a",
        "[class*='email'] a",
        "[class*='rcEmail'] a",
    ]
    DETAIL_PHONE: list[str] = [
        "a[href^='tel:']",
        ".phoneNum a",
        "[class*='phone'] a",
        "[class*='mobile'] a",
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
        jobs = await self._paginate_and_collect(page, f)
        if jobs:
            # Second pass: visit job detail pages to extract recruiter contact info
            jobs = await self._enrich_leads_batch(page, jobs)
        return jobs

    async def _is_hard_captcha(self, page: Page) -> bool:
        """
        Detect an explicit user-facing CAPTCHA challenge.
        Checks selectors visible from the MAIN frame (not inside iframes).
        """
        hard_sels = [
            "#challenge-form",                           # Cloudflare challenge
            "iframe[src*='challenges.cloudflare.com']",  # Cloudflare turnstile
            "iframe[src*='google.com/recaptcha/api2']",  # Google reCAPTCHA v2 iframe
            "iframe[src*='recaptcha.net/recaptcha']",    # reCAPTCHA alternate CDN
            "iframe[title*='reCAPTCHA']",                # reCAPTCHA by title attr
        ]
        for sel in hard_sels:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        # Also check body text as a last resort (Naukri's custom CAPTCHA page)
        try:
            body = await page.inner_text("body")
            if "I'm not a robot" in body or "check the box to let us know you" in body:
                return True
        except Exception:
            pass
        return False

    async def _save_captcha_artifacts(self, page: Page, page_num: int) -> dict:
        """Save screenshot + full HTML when CAPTCHA is detected. Returns paths dict."""
        paths: dict = {}
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        try:
            _DEBUG_SCREENSHOT.mkdir(parents=True, exist_ok=True)
            ss_path = _DEBUG_SCREENSHOT / "naukri_captcha.png"
            await page.screenshot(path=str(ss_path), full_page=False)
            paths["screenshot"] = str(ss_path.resolve())
            logger.info("captcha_screenshot_saved", path=paths["screenshot"])
        except Exception as exc:
            logger.debug("captcha_screenshot_failed", error=str(exc))
        try:
            _DEBUG_HTML.mkdir(parents=True, exist_ok=True)
            html_path = _DEBUG_HTML / "naukri_captcha.html"
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")
            paths["html"] = str(html_path.resolve())
            logger.info("captcha_html_saved", path=paths["html"], page=page_num)
        except Exception as exc:
            logger.debug("captcha_html_failed", error=str(exc))
        return paths

    async def _detect_login_status(self, page: Page) -> str:
        """Return 'logged_in', 'logged_out', or 'unknown' from page DOM."""
        try:
            # Logged-in indicators: user avatar / profile icon
            for sel in ("a.nI-gNb-drawer__icon", "[class*='nI-gNb-user']",
                        "[class*='user-name']", "[class*='profileIcon']"):
                if await page.query_selector(sel):
                    return "logged_in"
            # Logged-out indicators: Login / Register buttons
            for sel in ("a:has-text('Login')", "button:has-text('Login')",
                        "a:has-text('Register')", ".login-layer"):
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return "logged_out"
        except Exception:
            pass
        return "unknown"

    async def _paginate_and_collect(
        self, page: Page, f: FiltersConfig
    ) -> list[NaukriScrapedJob]:
        """
        Paginate through Naukri results using pageNo=1,2,3,…

        Naukri shows 20 jobs per page with URL-based pagination (no infinite scroll).
        Delays: 3-6 s between pages; 8-14 s every 10 pages to avoid bot detection.

        Stops when:
        • CAPTCHA / robot-verification detected  → saves screenshot+HTML, stops
        • URL redirected to auth / challenge page → stops
        • No cards found on 2 consecutive pages  → results exhausted
        • Safety cap reached (max_jobs or 5 000)
        """
        all_jobs:        list[NaukriScrapedJob] = []
        seen_urls:       set[str]               = set()
        page_num:        int                    = 1
        safety_cap:      int                    = f.max_jobs if f.max_jobs > 0 else 5_000
        empty_pages:     int                    = 0
        pretty_base_url: str                    = ""   # set after first-page redirect
        job_age_val:     str                    = _DATE_MAP.get(f.search_window_hours, "")

        logger.info(
            "search_started",
            source              = "naukri",
            keyword             = f.keyword,
            location            = f.location,
            job_type            = f.job_type,
            work_mode           = f.work_mode,
            max_jobs            = f.max_jobs,
            search_window_hours = f.search_window_hours,
            safety_cap          = safety_cap,
        )

        while len(all_jobs) < safety_cap:
            # ── Build URL for this page ────────────────────────────────────────
            # After first-page navigation Naukri redirects to a "pretty URL" and
            # strips all query params (including jobAge).  We store that pretty
            # base after page 1 and construct all subsequent URLs ourselves to
            # keep the jobAge filter intact.
            if pretty_base_url:
                if page_num > 1:
                    search_url = (
                        f"{pretty_base_url}-{page_num}?jobAge={job_age_val}"
                        if job_age_val else f"{pretty_base_url}-{page_num}"
                    )
                else:
                    search_url = (
                        f"{pretty_base_url}?jobAge={job_age_val}"
                        if job_age_val else pretty_base_url
                    )
            else:
                search_url = self._build_search_url(f, page_no=page_num)

            logger.info(
                "naukri_page_navigating",
                page_number       = page_num,
                search_url        = search_url,
                collected_so_far  = len(all_jobs),
                remaining_cap     = safety_cap - len(all_jobs),
            )
            logger.info("generated_search_url",
                        url=search_url, page=page_num,
                        search_window_hours=f.search_window_hours)

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                if page_num == 1:
                    logger.error("naukri_navigation_failed",
                                 page_number=page_num, search_url=search_url, error=str(exc))
                    raise
                logger.warning("naukri_page_nav_failed",
                               page_number=page_num, search_url=search_url, error=str(exc))
                break

            await _delay(page, 2_500, 4_000)

            # ── First-page: validate that the search filter was actually applied ──
            if page_num == 1 and not pretty_base_url:
                _init_url = page.url
                _raw_base = _init_url.split("?")[0].rstrip("/")
                _filter_ok = bool(job_age_val and f"jobAge={job_age_val}" in _init_url)

                if job_age_val and not _filter_ok:
                    # Root cause: Naukri redirects the query-param URL to a pretty URL
                    # and strips all parameters including jobAge.  Re-apply the filter.
                    _retry_url = f"{_raw_base}?jobAge={job_age_val}"
                    logger.warning(
                        "search_filter_not_applied",
                        note           = "Naukri redirect stripped jobAge; retrying with filter on pretty URL",
                        generated_url  = search_url,
                        after_redirect = _init_url,
                        retry_url      = _retry_url,
                    )
                    try:
                        await page.goto(_retry_url, wait_until="domcontentloaded", timeout=30_000)
                        await _delay(page, 2_500, 4_000)
                    except Exception as _exc:
                        logger.error("naukri_filter_retry_failed", error=str(_exc))
                    _filter_ok = f"jobAge={job_age_val}" in page.url

                # Store the pretty base URL (no query params) for all subsequent pages
                pretty_base_url = page.url.split("?")[0].rstrip("/")

                # ── Extract total reported job count from page title ────────────
                _title = ""
                try:
                    _title = await page.title()
                except Exception:
                    pass
                _total_naukri = 0
                _cm = re.search(r"([\d,]+)\s+\w[\w\s]*\s+Job\s+Vacanc", _title)
                if _cm:
                    _total_naukri = int(_cm.group(1).replace(",", ""))
                if not _total_naukri:
                    try:
                        _cnt_el = await page.query_selector(
                            ".search-type-heading, .srp-count, [class*='srp-count'], "
                            "[class*='count'][class*='jobs']"
                        )
                        if _cnt_el:
                            _ct = (await _cnt_el.inner_text()).strip()
                            _nm = re.search(r"[\d,]+", _ct)
                            if _nm:
                                _total_naukri = int(_nm.group(0).replace(",", ""))
                    except Exception:
                        pass

                _pages_detected = (_total_naukri // 20 + 1) if _total_naukri else "unknown"

                logger.info(
                    "naukri_search_diagnostics",
                    generated_search_url          = search_url,
                    final_search_url              = page.url,
                    current_page_url              = page.url,
                    search_window_hours           = f.search_window_hours,
                    total_jobs_reported_by_naukri = _total_naukri,
                    total_pages_detected          = _pages_detected,
                    expected_job_age              = job_age_val,
                    filter_applied                = _filter_ok,
                    pretty_base_url               = pretty_base_url,
                )

                # ── Raise if filter could not be applied ───────────────────────
                if job_age_val and not _filter_ok:
                    await self._screenshot(page, "naukri_search_page")
                    try:
                        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        _html = await page.content()
                        (_DEBUG_DIR / "naukri_search_page.html").write_text(_html, encoding="utf-8")
                    except Exception:
                        pass
                    raise SearchFilterNotAppliedException(
                        f"Naukri search filter not applied after retry. "
                        f"Expected jobAge={job_age_val} "
                        f"(search_window_hours={f.search_window_hours}) "
                        f"in URL but URL is: {page.url}"
                    )

                # ── Warn if total count implies no filter ──────────────────────
                if _total_naukri > 5000 and job_age_val:
                    logger.warning(
                        "naukri_filter_warning",
                        total_jobs_count = _total_naukri,
                        message = (
                            f"Naukri reports {_total_naukri} jobs with "
                            f"search_window_hours={f.search_window_hours}. "
                            "Count > 5000 may indicate the time filter is not active."
                        ),
                    )

            # ── Per-page diagnostics ───────────────────────────────────────────
            current_url  = page.url
            page_title   = ""
            login_status = "unknown"

            try:
                page_title = await page.title()
            except Exception:
                pass

            login_status = await self._detect_login_status(page)

            # Check body text for robot / CAPTCHA phrases
            body_text = ""
            try:
                body_text = (await page.inner_text("body")).lower()
            except Exception:
                pass

            robot_phrases = [
                "i'm not a robot", "i am not a robot",
                "not a robot", "robot verification",
                "verify you are human", "verify that you are human",
                "check the box to let us know",
            ]
            robot_verification_detected = any(p in body_text for p in robot_phrases)
            captcha_detected            = await self._is_hard_captcha(page)

            # Check whether the job listing container is present
            jobs_container_found  = False
            jobs_container_selector = ""
            for sel in _Sel.CONTAINER:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        jobs_container_found    = True
                        jobs_container_selector = sel
                        break
                except Exception:
                    continue

            logger.info(
                "naukri_page_diagnostics",
                page_number                = page_num,
                current_url               = current_url,
                page_title                = page_title,
                login_status              = login_status,
                search_url                = search_url,
                captcha_detected          = captcha_detected,
                robot_verification_detected = robot_verification_detected,
                jobs_container_found      = jobs_container_found,
                jobs_container_selector   = jobs_container_selector,
                jobs_found_count          = len(all_jobs),
            )

            # ── URL-level block detection ──────────────────────────────────────
            if any(pat in current_url for pat in ("/challenge", "/authwall", "/nlogin/login")):
                await self._save_captcha_artifacts(page, page_num)
                logger.warning(
                    "naukri_url_blocked",
                    page_number  = page_num,
                    current_url  = current_url,
                    page_title   = page_title,
                    login_status = login_status,
                    total_collected = len(all_jobs),
                    pagination_status = "stopped_url_blocked",
                )
                break

            # ── CAPTCHA / robot-verification detection ─────────────────────────
            if captcha_detected or robot_verification_detected:
                artifact_paths = await self._save_captcha_artifacts(page, page_num)
                logger.warning(
                    "naukri_captcha_detected",
                    status                     = "captcha_detected",
                    source                     = "naukri",
                    message                    = "Manual verification required",
                    page_number                = page_num,
                    current_url               = current_url,
                    page_title                = page_title,
                    login_status              = login_status,
                    captcha_detected          = captcha_detected,
                    robot_verification_detected = robot_verification_detected,
                    jobs_collected_before_captcha = len(all_jobs),
                    screenshot                = artifact_paths.get("screenshot", ""),
                    html                      = artifact_paths.get("html", ""),
                    pagination_status         = "stopped_captcha",
                )
                break

            # ── Proceed: dismiss overlays, scroll, extract ─────────────────────
            await self._dismiss_overlays(page)

            if page_num == 1:
                await self._screenshot(page, "naukri_page_01_after_dismiss")

            await self._scroll_results(page)

            remaining = safety_cap - len(all_jobs)
            page_jobs = await self._extract_cards(page, remaining, seen_urls)

            new_this_page = 0
            if not page_jobs:
                empty_pages += 1
                logger.info(
                    "naukri_page_empty",
                    page_number       = page_num,
                    consecutive_empty = empty_pages,
                    jobs_found_count  = len(all_jobs),
                    pagination_status = "empty_page",
                )
                if empty_pages >= 2:
                    logger.info(
                        "naukri_results_exhausted",
                        page_number       = page_num,
                        consecutive_empty = empty_pages,
                        total_collected   = len(all_jobs),
                        pagination_status = "stopped_no_more_results",
                    )
                    break
            else:
                empty_pages = 0
                for j in page_jobs:
                    url = (j.job_url or "").split("?")[0].rstrip("/").lower()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(j)
                        new_this_page += 1

            logger.info(
                "naukri_page_done",
                page_number       = page_num,
                jobs_this_page    = len(page_jobs),
                new_unique_jobs   = new_this_page,
                total_jobs        = len(all_jobs),
                pagination_status = "continuing" if len(all_jobs) < safety_cap else "cap_reached",
            )

            page_num += 1

            if page_num % 10 == 0:
                logger.info("naukri_long_pause", page_number=page_num, total=len(all_jobs))
                await _delay(page, 8_000, 14_000)
            else:
                await _delay(page, 3_000, 6_000)

        logger.info(
            "pagination_completed",
            source            = "naukri",
            pages_visited     = page_num - 1,
            total_jobs        = len(all_jobs),
            pagination_status = "done",
        )
        logger.info("jobs_found", source="naukri", total=len(all_jobs))
        return all_jobs

    async def _enrich_leads_batch(
        self, page: Page, jobs: list[NaukriScrapedJob], max_enrich: int = 200
    ) -> list[NaukriScrapedJob]:
        """
        Second pass: open job detail URLs and extract recruiter / HR contact.
        Only visits jobs without recruiter data from the list-card pass.
        Capped at max_enrich visits to keep run time bounded.
        """
        candidates = [
            (idx, job) for idx, job in enumerate(jobs)
            if job.job_url and not (job.recruiter_name and (job.email_id or job.contact_number))
        ]
        to_visit = candidates[:max_enrich]
        total    = len(jobs)
        enriched = 0

        logger.info(
            "naukri_lead_enrichment_started",
            total=total, candidates=len(candidates), visiting=len(to_visit),
        )

        for idx, job in to_visit:
            try:
                await page.goto(job.job_url, wait_until="domcontentloaded", timeout=20_000)
                await _delay(page, 400, 600)
                logger.info("job_opened", source="naukri", index=idx, url=job.job_url)
                await self._dismiss_overlays(page)

                name    = _clean(await _first_text(page, _Sel.DETAIL_RECRUITER_NAME)) or None
                desig   = _clean(await _first_text(page, _Sel.DETAIL_RECRUITER_DESIGNATION)) or None
                company = _clean(await _first_text(page, _Sel.DETAIL_RECRUITER_COMPANY)) or None

                email_raw = await _first_attr(page, _Sel.DETAIL_EMAIL, "href")
                email = email_raw.replace("mailto:", "").strip() if email_raw else (
                    _clean(await _first_text(page, _Sel.DETAIL_EMAIL)) or None
                )
                phone_raw = await _first_attr(page, _Sel.DETAIL_PHONE, "href")
                phone = phone_raw.replace("tel:", "").strip() if phone_raw else None

                # Filter out known system/fraud-report addresses
                _JUNK_EMAIL_PATTERNS = ("reportfraud", "noreply", "no-reply", "fraud@", "abuse@")
                if email and any(p in email.lower() for p in _JUNK_EMAIL_PATTERNS):
                    email = None

                if name:
                    job.recruiter_name = name
                    logger.info("recruiter_found", source="naukri", name=name, index=idx)
                if desig:
                    job.job_poster_designation = desig
                    logger.info("designation_found", source="naukri", designation=desig, index=idx)
                if company and not job.recruiter_company:
                    job.recruiter_company = company
                if email:
                    job.email_id = email
                    logger.info("email_found", source="naukri", email=email, index=idx)
                if phone:
                    job.contact_number = phone
                    logger.info("phone_found", source="naukri", phone=phone, index=idx)

                if name or email or phone:
                    enriched += 1
                    logger.info(
                        "lead_record_created",
                        source  = "naukri",
                        index   = idx,
                        name    = name,
                        email   = email,
                        phone   = phone,
                        company = company,
                    )

            except Exception as exc:
                logger.debug("naukri_lead_enrich_failed", index=idx, url=job.job_url, error=str(exc))

        logger.info(
            "naukri_lead_enrichment_complete",
            total=total, visited=len(to_visit), enriched=enriched,
        )
        return jobs


    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        # Press Escape first — closes most Naukri modals (login prompt,
        # "Register for free", app download banners, etc.) without needing a selector
        try:
            await page.keyboard.press("Escape")
            await _delay(page, 300, 500)
        except Exception:
            pass

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

        # Second Escape in case a second modal appeared (e.g. cookie then login)
        try:
            await page.keyboard.press("Escape")
            await _delay(page, 200, 300)
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
        """
        Extract job cards from the current page.
        Strategy: CSS selectors first → JS link-scan fallback.
        Logs selector used, card count, and extraction method at every step.
        """
        if seen_urls is None:
            seen_urls = set()

        # ── CSS selector probe: wait up to 5 s for any card selector to appear ──
        matched_selector = ""
        for sel in _Sel.CARD:
            try:
                await page.wait_for_selector(sel, timeout=5_000)
                matched_selector = sel
                logger.info("naukri_card_selector_matched", selector=sel)
                break
            except Exception:
                continue

        if not matched_selector:
            logger.info("naukri_no_card_selector_matched",
                        selectors_tried=len(_Sel.CARD),
                        note="all CSS selectors timed out — falling back to JS extraction")

        # ── CSS query: find the first selector that returns ≥1 elements ─────────
        raw: list[ElementHandle] = []
        css_selector_used = ""
        for sel in _Sel.CARD:
            found = await page.query_selector_all(sel)
            cnt   = len(found)
            logger.info("naukri_selector_tried", selector=sel, count=cnt)
            if found:
                raw               = found
                css_selector_used = sel
                logger.info(
                    "naukri_cards_found",
                    selector         = sel,
                    count            = cnt,
                    extraction_method = "css",
                )
                break

        if not raw:
            # ── Zero CSS hits → screenshot + JS fallback ─────────────────────
            await self._screenshot(page, "naukri_no_cards")
            logger.warning(
                "naukri_no_css_cards",
                selectors_tried   = len(_Sel.CARD),
                jobs_found_count  = 0,
                extraction_method = "js_fallback",
                note              = "CSS selectors returned 0 elements; trying JS link-scan",
            )
            js_jobs = await self._js_extract_jobs(page, remaining, seen_urls)
            logger.info(
                "naukri_js_extraction_result",
                jobs_found_count  = len(js_jobs),
                extraction_method = "js",
            )
            return js_jobs

        # ── CSS elements found → parse into NaukriScrapedJob objects ────────────
        parsed = await self._parse_card_elements(raw, remaining, seen_urls)
        logger.info(
            "naukri_card_parse_result",
            css_elements_found = len(raw),
            selector_used      = css_selector_used,
            jobs_parsed        = len(parsed),
            jobs_found_count   = len(parsed),
            extraction_method  = "css",
        )

        if not parsed:
            # CSS elements exist but none parsed (different DOM structure for logged-out view)
            logger.info(
                "naukri_css_parse_empty",
                css_elements     = len(raw),
                selector_used    = css_selector_used,
                extraction_method = "js_fallback",
                note             = "CSS found elements but title/URL extraction empty — trying JS",
            )
            js_jobs = await self._js_extract_jobs(page, remaining, seen_urls)
            logger.info(
                "naukri_js_extraction_result",
                jobs_found_count  = len(js_jobs),
                extraction_method = "js",
            )
            return js_jobs

        return parsed

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
            logger.warning(
                "naukri_js_extraction_no_jobs",
                jobs_found_count  = 0,
                extraction_method = "js",
                note = "JS found 0 job anchors — page may be a CAPTCHA or non-results page",
            )
            return []

        logger.info(
            "naukri_js_extraction_found",
            count             = len(raw_jobs),
            jobs_found_count  = len(raw_jobs),
            extraction_method = "js",
        )
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

            # Recruiter / lead intelligence (optional, may not appear on all cards)
            recruiter_name    = _clean(await _first_text(el, _Sel.RECRUITER_NAME)) or None
            recruiter_company = _clean(await _first_text(el, _Sel.RECRUITER_COMPANY)) or None
            email_raw         = await _first_attr(el, _Sel.RECRUITER_EMAIL, "href")
            email_id          = email_raw.replace("mailto:", "").strip() if email_raw else (
                                _clean(await _first_text(el, _Sel.RECRUITER_EMAIL)) or None
                                )
            phone_raw         = await _first_attr(el, _Sel.RECRUITER_PHONE, "href")
            contact_number    = phone_raw.replace("tel:", "").strip() if phone_raw else (
                                _clean(await _first_text(el, _Sel.RECRUITER_PHONE)) or None
                                )

            job = NaukriScrapedJob(
                job_title              = title,
                company                = company,
                location               = location,
                salary                 = salary,
                experience             = experience,
                posted_date            = _format_posted(posted_raw),
                job_url                = clean_url,
                job_description        = description,
                skills                 = skills[:15],
                work_mode              = _infer_work_mode(location),
                source                 = "Naukri",
                recruiter_name         = recruiter_name,
                recruiter_company      = recruiter_company,
                job_poster_designation = None,
                email_id               = email_id,
                contact_number         = contact_number,
            )
            logger.info("job_found", source="naukri", title=title, company=company, url=clean_url)
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
