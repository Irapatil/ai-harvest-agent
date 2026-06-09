"""ScraperAgent: CSS/XPath-driven structured extractor (no agentic loop)."""
from __future__ import annotations

from typing import Any

import structlog
from bs4 import BeautifulSoup

from app.agents.base_agent import BaseAgent
from app.models.agent import AgentType
from app.prompts.extraction_prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

logger = structlog.get_logger(__name__)


class ScraperAgent(BaseAgent):
    """
    Deterministic scraper that:
      1. Navigates to URL
      2. Asks Claude to write CSS selectors for the goal
      3. Applies selectors via BeautifulSoup
      4. Returns structured list of records
    """

    agent_type = AgentType.SCRAPER

    async def plan(self, url: str, goal: str) -> dict[str, Any]:
        """Ask Claude which CSS selectors to use."""
        snapshot = await self._playwright.navigate(url)
        self._visited(url)

        prompt = build_extraction_prompt(
            url=url,
            goal=goal,
            html_sample=snapshot.html[:8_000],
        )
        selectors = await self._llm.extract_json(
            content=snapshot.html[:8_000],
            schema_description=(
                "Return a JSON object with keys being field names and values being "
                "CSS selectors that extract each field from the page HTML."
            ),
            system=EXTRACTION_SYSTEM_PROMPT,
        )
        return {"selectors": selectors, "snapshot": snapshot, "url": url}

    async def execute(
        self, url: str, goal: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply selectors to each page up to max_pages."""
        selectors: dict[str, str] = plan["selectors"]
        snapshot = plan["snapshot"]
        records: list[dict[str, Any]] = []
        page_num = 0

        while snapshot and page_num < self._config.max_iterations:
            page_num += 1
            extracted = self._apply_selectors(snapshot.html, selectors)
            records.extend(extracted)
            self._record_step(
                "extract",
                {"url": snapshot.url, "selectors": selectors},
                observation=f"Extracted {len(extracted)} records",
            )

            # Look for a "next page" link
            next_url = self._find_next_page(snapshot.html, snapshot.url)
            if not next_url:
                break
            snapshot = await self._playwright.navigate(next_url)
            self._visited(next_url)

        return {"records": records, "pages_scraped": page_num}

    async def reflect(
        self, raw_results: dict[str, Any], goal: str
    ) -> dict[str, Any]:
        """Deduplicate and validate the records."""
        records: list[dict[str, Any]] = raw_results.get("records", [])
        # Remove complete duplicates
        seen = set()
        unique = []
        for r in records:
            key = str(sorted(r.items()))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return {
            "records": unique,
            "total": len(unique),
            "pages_scraped": raw_results.get("pages_scraped", 0),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_selectors(
        html: str, selectors: dict[str, str]
    ) -> list[dict[str, str]]:
        """Extract records using CSS selectors via BeautifulSoup."""
        soup = BeautifulSoup(html, "lxml")

        # Determine item container: find common parent of all selectors
        # Simple heuristic: use first selector's parent
        first_sel = next(iter(selectors.values()), None)
        if not first_sel:
            return []

        # Find all top-level containers
        container_sel = first_sel.rsplit(" ", 1)[0] if " " in first_sel else first_sel
        containers = soup.select(container_sel)
        if not containers:
            containers = [soup]

        records: list[dict[str, str]] = []
        for container in containers:
            record: dict[str, str] = {}
            for field, sel in selectors.items():
                # Try relative selector within container first
                relative_sel = sel.split(" ")[-1]  # leaf selector
                el = container.select_one(relative_sel) or soup.select_one(sel)
                record[field] = el.get_text(strip=True) if el else ""
            if any(record.values()):
                records.append(record)

        return records

    @staticmethod
    def _find_next_page(html: str, current_url: str) -> str | None:
        """Look for rel='next' or text 'Next' links."""
        soup = BeautifulSoup(html, "lxml")
        # 1. rel="next"
        link = soup.find("a", rel=lambda r: r and "next" in r)  # type: ignore[arg-type]
        if link and link.get("href"):  # type: ignore[union-attr]
            href = link["href"]  # type: ignore[index]
            if isinstance(href, str) and href.startswith("http"):
                return href
        # 2. text "Next"
        for a in soup.find_all("a"):
            if a.get_text(strip=True).lower() in ("next", "next →", "next page", ">"):
                href = a.get("href", "")
                if isinstance(href, str) and href.startswith("http"):
                    return href
        return None
