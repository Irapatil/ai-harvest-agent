"""
LinkedIn Jobs Scraper — "Contract Java Developer" · Past 24 hours
=================================================================

Two-phase approach
──────────────────
Phase A  URL shortcut
    Navigate directly to a pre-filtered LinkedIn jobs URL.
    f_TPR=r86400 → "Date Posted: Past 24 hours" (86 400 s)
    sortBy=DD    → newest first

Phase B  UI interaction (fallback / validation)
    If Phase A yields 0 cards we fall back to:
      1. Open linkedin.com/jobs
      2. Type the query into the search box
      3. Submit → wait for results
      4. Click "Date Posted" → "Past 24 hours"

Extraction
──────────
For every job card we pull:
    • job_title   – <h3.base-search-card__title>
    • company     – <h4.base-search-card__subtitle>
    • location    – <span.job-search-card__location>

Output
──────
    • Pretty table printed to stdout
    • linkedin_java_contract_jobs.json saved to cwd

Run
───
    pip install playwright
    playwright install chromium

    python scripts/scrape_linkedin_java_contracts.py
    python scripts/scrape_linkedin_java_contracts.py "Senior Java Contract" 3
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

KEYWORDS     = "Contract Java Developer"
MAX_PAGES    = 5        # ~25 cards per LinkedIn page → up to ~125 jobs
HEADLESS     = True     # False → watch the browser for debugging
SLOW_MO      = 500      # ms between every Playwright action (anti-bot pacing)
TIMEOUT      = 30_000   # ms — page / element load timeout


# ══════════════════════════════════════════════════════════════════════════════
# Selectors
# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn periodically rewrites its markup.  Every slot has an ordered list
# of candidates; the first one that matches wins.

# ── Phase A: URL-based search result page ────────────────────────────────────
CARD_SEL = [
    "ul.jobs-search__results-list li",   # standard public results list
    "li[data-occludable-job-id]",        # data-attribute variant
    "div.job-search-card",               # card div fallback
]

TITLE_SEL    = ["h3.base-search-card__title",       "a.job-card-list__title",              "[class*='job-card'] h3"]
COMPANY_SEL  = ["h4.base-search-card__subtitle",    "a.job-card-container__company-name",  "h4 a"]
LOCATION_SEL = ["span.job-search-card__location",   "span.job-card-container__metadata-item"]
LINK_SEL     = ["a.base-card__full-link",            "a[href*='/jobs/view/']"]
TIME_SEL     = ["time"]

# ── Phase B: UI interaction ───────────────────────────────────────────────────
SEARCH_BOX_SEL   = [
    'input[aria-label="Search by title, skill, or company"]',
    'input[id="job-search-bar-keywords"]',
    'input[name="keywords"]',
    'input[placeholder*="itle"]',         # "title, skill…"
    'input[type="text"]',
]
SEARCH_BTN_SEL   = [
    'button[type="submit"]',
    'button[aria-label="Search"]',
    'button.jobs-search-box__submit-button',
]
FILTER_DATE_SEL  = [
    'button[aria-label*="Date posted"]',
    'button[aria-label*="date"]',
    'li button:has-text("Date posted")',
    'button:has-text("Date posted")',
]
PAST_24H_SEL     = [
    'label[for*="24"]',
    'span:has-text("Past 24 hours")',
    'li:has-text("Past 24 hours")',
    'a:has-text("Past 24 hours")',
]
APPLY_FILTER_SEL = [
    'button:has-text("Show results")',
    'button:has-text("Apply")',
    'button[data-control-name="filter_show_results"]',
]

# ── Overlays to dismiss ───────────────────────────────────────────────────────
COOKIE_SEL  = ['button[action-type="ACCEPT"]', "#artdeco-global-alert-container button"]
MODAL_SEL   = ['button.modal__dismiss', 'button[aria-label="Dismiss"]',
               'button[data-tracking-control-name="public_jobs_guest-alert-dismiss"]']

# ── Pagination ────────────────────────────────────────────────────────────────
NEXT_SEL = [
    'button[aria-label="Next"]',
    'button.artdeco-pagination__button--next',
]


# ══════════════════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Job:
    job_title:   str
    company:     str
    location:    str
    job_url:     str = ""
    posted_time: str = ""
    job_id:      str = ""


# ══════════════════════════════════════════════════════════════════════════════
# Low-level helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _first(root, selectors: list[str], *, attr: str | None = None) -> str:
    """Return inner_text (or *attr*) of the first matching element, or ''."""
    for sel in selectors:
        try:
            el = await root.query_selector(sel)
            if not el:
                continue
            if attr:
                return ((await el.get_attribute(attr)) or "").strip()
            return ((await el.inner_text()) or "").strip()
        except Exception:
            continue
    return ""


async def _dismiss(page: Page, selectors: list[str]) -> None:
    """Best-effort: click the first visible button from *selectors*."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                await loc.click()
                await page.wait_for_timeout(700)
                return
        except Exception:
            continue


async def _wait_cards(page: Page) -> bool:
    """Block until at least one job card is visible; return False on timeout."""
    for sel in CARD_SEL:
        try:
            await page.wait_for_selector(sel, timeout=TIMEOUT)
            return True
        except Exception:
            continue
    return False


async def _next_page(page: Page) -> bool:
    """Click Next; return True if navigation happened."""
    for sel in NEXT_SEL:
        try:
            btn = page.locator(sel).first
            if not await btn.is_visible(timeout=3_000):
                continue
            if await btn.get_attribute("disabled") is not None:
                return False
            await btn.click()
            await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT)
            await page.wait_for_timeout(2_200)
            return True
        except Exception:
            continue
    return False


def _job_id(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url or "")
    return m.group(1) if m else ""


def _clean_url(url: str) -> str:
    return url.split("?")[0] if url and "?" in url else url


def _dedup(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    out:  list[Job] = []
    for j in jobs:
        key = j.job_id or f"{j.job_title}|{j.company}|{j.location}"
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Card extraction
# ══════════════════════════════════════════════════════════════════════════════

async def _extract_page_cards(page: Page) -> list[Job]:
    """Parse every job card visible on the current results page."""
    raw_cards: list = []
    for sel in CARD_SEL:
        raw_cards = await page.query_selector_all(sel)
        if raw_cards:
            break

    jobs: list[Job] = []
    for card in raw_cards:
        try:
            title    = await _first(card, TITLE_SEL)
            company  = await _first(card, COMPANY_SEL)
            location = await _first(card, LOCATION_SEL)
            url      = await _first(card, LINK_SEL, attr="href")
            posted   = await _first(card, TIME_SEL, attr="datetime")

            if not title:          # empty card (ad slot, separator, etc.)
                continue

            url = _clean_url(url)
            jobs.append(Job(
                job_title   = title,
                company     = company,
                location    = location,
                job_url     = url,
                posted_time = posted,
                job_id      = _job_id(url),
            ))
        except Exception as exc:
            print(f"  [warn] card parse: {exc}", file=sys.stderr)

    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Phase A — URL shortcut (primary path)
# ══════════════════════════════════════════════════════════════════════════════

def _build_url(keywords: str) -> str:
    """
    Build the LinkedIn public jobs search URL with:
      • keywords    — URL-encoded search string
      • f_TPR=r86400 — "Past 24 hours" filter
      • sortBy=DD   — newest first
    """
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(keywords)}"
        "&f_TPR=r86400"
        "&sortBy=DD"
        "&position=1"
        "&pageNum=0"
    )


async def _scrape_via_url(
    page: Page,
    keywords: str,
    max_pages: int,
) -> list[Job]:
    """Navigate to a pre-built URL with the 24-hour filter already embedded."""
    url = _build_url(keywords)
    print(f"  → URL  : {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
    await page.wait_for_timeout(1_800)
    await _dismiss(page, COOKIE_SEL)
    await _dismiss(page, MODAL_SEL)
    await page.wait_for_timeout(800)
    return await _paginate(page, max_pages)


# ══════════════════════════════════════════════════════════════════════════════
# Phase B — UI interaction (fallback)
# ══════════════════════════════════════════════════════════════════════════════

async def _scrape_via_ui(
    page: Page,
    keywords: str,
    max_pages: int,
) -> list[Job]:
    """
    Fully interactive path:
      1. Open linkedin.com/jobs
      2. Type keywords into the search box and submit
      3. Open "Date Posted" filter → select "Past 24 hours" → Apply
      4. Scrape result cards
    """
    # ── 1. Open LinkedIn Jobs home ────────────────────────────────────────────
    print("  → Navigating to linkedin.com/jobs …")
    await page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded",
                    timeout=TIMEOUT)
    await page.wait_for_timeout(1_500)
    await _dismiss(page, COOKIE_SEL)
    await _dismiss(page, MODAL_SEL)

    # ── 2. Type in the search box ─────────────────────────────────────────────
    print(f"  → Searching for: '{keywords}' …")
    for sel in SEARCH_BOX_SEL:
        try:
            box = page.locator(sel).first
            if await box.is_visible(timeout=4_000):
                await box.click()
                await box.fill("")
                await box.type(keywords, delay=80)   # human-speed typing
                await page.wait_for_timeout(500)
                break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not find the LinkedIn search input.")

    # ── 3. Submit search ──────────────────────────────────────────────────────
    submitted = False
    for sel in SEARCH_BTN_SEL:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                submitted = True
                break
        except Exception:
            continue
    if not submitted:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT)
    await page.wait_for_timeout(2_000)

    # ── 4. Apply "Date Posted → Past 24 hours" filter ─────────────────────────
    print("  → Applying 'Past 24 hours' filter …")
    filter_opened = False
    for sel in FILTER_DATE_SEL:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=4_000):
                await btn.click()
                await page.wait_for_timeout(800)
                filter_opened = True
                break
        except Exception:
            continue

    if filter_opened:
        for sel in PAST_24H_SEL:
            try:
                opt = page.locator(sel).first
                if await opt.is_visible(timeout=3_000):
                    await opt.click()
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        for sel in APPLY_FILTER_SEL:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3_000):
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT)
                    await page.wait_for_timeout(2_000)
                    break
            except Exception:
                continue
    else:
        print("  [warn] Could not open 'Date Posted' filter — results may span > 24 h")

    return await _paginate(page, max_pages)


# ══════════════════════════════════════════════════════════════════════════════
# Pagination loop (shared by both phases)
# ══════════════════════════════════════════════════════════════════════════════

async def _paginate(page: Page, max_pages: int) -> list[Job]:
    all_jobs: list[Job] = []

    for page_num in range(1, max_pages + 1):
        if not await _wait_cards(page):
            print(f"      Page {page_num}: no cards — stopping.")
            break

        cards = await _extract_page_cards(page)
        all_jobs.extend(cards)
        print(f"      Page {page_num:>2}: {len(cards):>3} cards  │  total so far: {len(all_jobs)}")

        if page_num == max_pages:
            break
        if not await _next_page(page):
            print("      Reached last page.")
            break

    return all_jobs


# ══════════════════════════════════════════════════════════════════════════════
# Browser setup
# ══════════════════════════════════════════════════════════════════════════════

async def _launch() -> tuple:
    """Launch Chromium with anti-detection settings; return (playwright, browser, context, page)."""
    pw      = await async_playwright().start()
    browser: Browser = await pw.chromium.launch(
        headless=HEADLESS,
        slow_mo=SLOW_MO,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context: BrowserContext = await browser.new_context(
        viewport    = {"width": 1366, "height": 900},
        user_agent  = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale      = "en-US",
        timezone_id = "Europe/London",
    )
    # Remove the navigator.webdriver fingerprint
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page: Page = await context.new_page()
    return pw, browser, context, page


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

async def scrape(
    keywords:  str  = KEYWORDS,
    max_pages: int  = MAX_PAGES,
    ui_mode:   bool = False,
) -> list[Job]:
    """
    Scrape LinkedIn Jobs for *keywords* filtered to the past 24 hours.

    Args:
        keywords:  Search string.
        max_pages: Max LinkedIn result pages to walk (25 jobs each).
        ui_mode:   True → use the browser UI search + filter interaction.
                   False (default) → use the pre-filtered URL (faster).

    Returns:
        Deduplicated list of :class:`Job` dataclasses.
    """
    pw, browser, context, page = await _launch()

    try:
        if ui_mode:
            print("  Mode: UI interaction (search box + filter dropdown)")
            raw = await _scrape_via_ui(page, keywords, max_pages)
        else:
            print("  Mode: URL shortcut (f_TPR=r86400 filter embedded in URL)")
            raw = await _scrape_via_url(page, keywords, max_pages)

            # Fallback: if URL mode returned nothing, retry via UI
            if not raw:
                print("  URL mode returned 0 results — retrying via UI …")
                raw = await _scrape_via_ui(page, keywords, max_pages)
    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    unique = _dedup(raw)
    print(f"\n  ✓ {len(unique)} unique jobs (from {len(raw)} raw cards)")
    return unique


# ══════════════════════════════════════════════════════════════════════════════
# Output
# ══════════════════════════════════════════════════════════════════════════════

def print_table(jobs: list[Job]) -> None:
    TW, CW, LW = 42, 32, 28
    sep = "─" * (5 + TW + 2 + CW + 2 + LW)
    print(f"\n{sep}")
    print(f"{'#':<5}{'Job Title':<{TW}}  {'Company':<{CW}}  {'Location':<{LW}}")
    print(sep)
    for i, j in enumerate(jobs, 1):
        print(
            f"{i:<5}"
            f"{j.job_title[:TW]:<{TW}}  "
            f"{j.company[:CW]:<{CW}}  "
            f"{j.location[:LW]:<{LW}}"
        )
    print(sep)
    print(f"  {len(jobs)} jobs total\n")


def save_json(jobs: list[Job], path: Path = Path("linkedin_java_contract_jobs.json")) -> None:
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "keywords":   KEYWORDS,
        "filter":     "past_24_hours",
        "total":      len(jobs),
        "jobs": [
            {
                "job_title": j.job_title,
                "company":   j.company,
                "location":  j.location,
                "job_url":   j.job_url,
                "posted":    j.posted_time,
                "job_id":    j.job_id,
            }
            for j in jobs
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved → {path.resolve()}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    args = sys.argv[1:]

    # Parse optional trailing integer as max_pages
    max_pages = MAX_PAGES
    if args and args[-1].isdigit():
        max_pages = int(args.pop())

    keywords = " ".join(args) if args else KEYWORDS
    ui_mode  = "--ui" in keywords
    keywords = keywords.replace("--ui", "").strip()

    header = "─" * 62
    print(header)
    print("  LinkedIn Jobs Scraper")
    print(f"  Query    : {keywords}")
    print(f"  Filter   : Past 24 hours")
    print(f"  Max pages: {max_pages}  (~{max_pages * 25} jobs)")
    print(f"  Mode     : {'UI interaction' if ui_mode else 'URL shortcut (default)'}")
    print(header + "\n")

    jobs = await scrape(keywords=keywords, max_pages=max_pages, ui_mode=ui_mode)

    if not jobs:
        print(
            "No jobs found.\n"
            "Tips:\n"
            "  • Set HEADLESS = False in this script to watch the browser\n"
            "  • Try running with --ui flag: python script.py --ui\n"
            "  • LinkedIn may throttle guest access — wait and retry\n"
        )
        return

    print_table(jobs)
    save_json(jobs)


if __name__ == "__main__":
    asyncio.run(main())
