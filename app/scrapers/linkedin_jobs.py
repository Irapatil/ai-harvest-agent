"""
LinkedIn Jobs scraper using Playwright.

Searches for jobs by keyword, applies the "Past 24 hours" date filter,
and extracts: job_title, company, location for each result.

Usage (standalone):
    python -m app.scrapers.linkedin_jobs

Usage (as library):
    from app.scrapers.linkedin_jobs import scrape_linkedin_jobs
    import asyncio
    jobs = asyncio.run(scrape_linkedin_jobs("Contract Java Developer"))
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from typing import Optional

from playwright.async_api import Browser, Page, Playwright, async_playwright


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LinkedInJob:
    job_title: str
    company: str
    location: str
    job_url: str
    posted_time: str = ""
    job_id: str = ""


# ── Constants ─────────────────────────────────────────────────────────────────

BASE_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/?"
    "keywords={keywords}"
    "&f_TPR=r86400"   # r86400 = past 24 hours (86 400 seconds)
    "&position=1"
    "&pageNum=0"
)

# CSS selectors (as of 2025 — LinkedIn changes these periodically)
SELECTORS = {
    "job_cards":    "ul.jobs-search__results-list li",
    "job_title":    "h3.base-search-card__title",
    "company":      "h4.base-search-card__subtitle",
    "location":     "span.job-search-card__location",
    "link":         "a.base-card__full-link",
    "posted_time":  "time",
    "next_page":    'button[aria-label="Next"]',
    "cookie_accept": 'button[action-type="ACCEPT"]',
}


# ── Scraper ───────────────────────────────────────────────────────────────────

async def scrape_linkedin_jobs(
    keywords: str = "Contract Java Developer",
    max_pages: int = 3,
    headless: bool = True,
    slow_mo: int = 500,
) -> list[LinkedInJob]:
    """
    Main entry point.

    Args:
        keywords:  Job search keywords (e.g. "Contract Java Developer")
        max_pages: Maximum result pages to scrape (25 jobs per page)
        headless:  Run browser headlessly (set False to watch/debug)
        slow_mo:   Milliseconds between Playwright actions (reduces bot detection)

    Returns:
        List of LinkedInJob dataclass instances
    """
    url = BASE_SEARCH_URL.format(keywords=keywords.replace(" ", "%20"))
    jobs: list[LinkedInJob] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        # Mask webdriver flag
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print(f"[LinkedIn] Navigating to search: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(2000)

        for page_num in range(max_pages):
            print(f"[LinkedIn] Scraping page {page_num + 1}...")
            page_jobs = await _extract_jobs_from_page(page)
            jobs.extend(page_jobs)
            print(f"  → Found {len(page_jobs)} jobs (total: {len(jobs)})")

            if not await _go_to_next_page(page):
                print("[LinkedIn] No more pages.")
                break

        await browser.close()

    # Deduplicate by job_id
    seen: set[str] = set()
    unique: list[LinkedInJob] = []
    for j in jobs:
        key = j.job_id or f"{j.job_title}|{j.company}|{j.location}"
        if key not in seen:
            seen.add(key)
            unique.append(j)

    print(f"[LinkedIn] Done. {len(unique)} unique jobs extracted.")
    return unique


# ── Page helpers ──────────────────────────────────────────────────────────────

async def _dismiss_cookie_banner(page: Page) -> None:
    """Accept cookies if the consent banner appears."""
    try:
        btn = page.locator(SELECTORS["cookie_accept"]).first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await page.wait_for_timeout(1000)
    except Exception:
        pass  # Banner not present — continue


async def _extract_jobs_from_page(page: Page) -> list[LinkedInJob]:
    """Extract all job cards visible on the current search results page."""
    # Wait for cards to load
    try:
        await page.wait_for_selector(SELECTORS["job_cards"], timeout=10_000)
    except Exception:
        return []

    cards = await page.query_selector_all(SELECTORS["job_cards"])
    jobs: list[LinkedInJob] = []

    for card in cards:
        try:
            title_el  = await card.query_selector(SELECTORS["job_title"])
            company_el = await card.query_selector(SELECTORS["company"])
            location_el = await card.query_selector(SELECTORS["location"])
            link_el   = await card.query_selector(SELECTORS["link"])
            time_el   = await card.query_selector(SELECTORS["posted_time"])

            job_title = (await title_el.inner_text()).strip()   if title_el   else ""
            company   = (await company_el.inner_text()).strip() if company_el else ""
            location  = (await location_el.inner_text()).strip() if location_el else ""
            job_url   = await link_el.get_attribute("href")     if link_el    else ""
            posted    = (await time_el.get_attribute("datetime")) if time_el  else ""

            # Extract numeric job ID from URL
            job_id = ""
            if job_url:
                m = re.search(r"jobs/view/(\d+)", job_url or "")
                job_id = m.group(1) if m else ""
                # Normalise URL
                job_url = job_url.split("?")[0] if "?" in job_url else job_url

            if job_title:
                jobs.append(LinkedInJob(
                    job_title=job_title,
                    company=company,
                    location=location,
                    job_url=job_url or "",
                    posted_time=posted,
                    job_id=job_id,
                ))
        except Exception as exc:
            print(f"  [warn] Could not parse card: {exc}")
            continue

    return jobs


async def _go_to_next_page(page: Page) -> bool:
    """Click the 'Next' button if available. Returns True if navigated."""
    try:
        next_btn = page.locator(SELECTORS["next_page"]).first
        if not await next_btn.is_visible(timeout=3000):
            return False
        is_disabled = await next_btn.get_attribute("disabled")
        if is_disabled is not None:
            return False
        await next_btn.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _main() -> None:
    import sys

    keywords = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Contract Java Developer"
    print(f"Searching LinkedIn Jobs for: '{keywords}' (past 24 hours)")
    jobs = await scrape_linkedin_jobs(keywords=keywords, max_pages=3, headless=True)

    print(f"\n{'─'*70}")
    print(f"{'#':<4} {'Job Title':<35} {'Company':<25} {'Location':<20}")
    print(f"{'─'*70}")
    for i, job in enumerate(jobs, 1):
        print(f"{i:<4} {job.job_title[:34]:<35} {job.company[:24]:<25} {job.location[:19]:<20}")

    # Also save to JSON
    output_path = "linkedin_jobs_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([asdict(j) for j in jobs], f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(_main())
