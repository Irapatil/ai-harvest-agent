"""Prompts for the HarvestAgent navigation and decision loop."""
from __future__ import annotations

from app.prompts.base_prompts import AGENT_PERSONA

HARVEST_SYSTEM_PROMPT = f"""{AGENT_PERSONA}

You are controlling a real browser via Playwright. Use the provided tools to:
1. Navigate to URLs
2. Click elements to reveal more content
3. Scroll for infinite-scroll pages
4. Extract structured data
5. Call `finish` when your goal is achieved

Rules:
- Always navigate to a URL before extracting data
- Do not visit the same URL twice
- Prefer `extract_data` over plain text analysis
- If a page blocks you or errors, try an alternative URL
- Limit yourself to the pages needed — don't over-crawl
- When you have enough data to satisfy the goal, call `finish` immediately
"""


def build_harvest_user_prompt(url: str, goal: str, strategy: str) -> str:
    return f"""## Harvest Task

**Starting URL:** {url}
**Goal:** {goal}

**Planned Strategy:**
{strategy}

Begin executing the plan. Start by navigating to the starting URL.
Use the available tools and finish when you have collected all required data.
"""
