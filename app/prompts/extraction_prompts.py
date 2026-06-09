"""Prompts for structured data extraction from HTML content."""
from __future__ import annotations

from app.prompts.base_prompts import AGENT_PERSONA, truncate

EXTRACTION_SYSTEM_PROMPT = f"""{AGENT_PERSONA}

You are a data extraction specialist. Given HTML content and a goal,
produce precise CSS selectors or extract structured JSON directly.

Rules:
- Return only valid JSON — no markdown fences, no explanation
- Use specific, stable selectors (prefer id, data-* attrs over positional)
- If a field doesn't exist, use null — never hallucinate values
- Normalize whitespace in text fields
"""


def build_extraction_prompt(url: str, goal: str, html_sample: str) -> str:
    return f"""URL: {url}
Goal: {goal}

HTML Sample (first 8000 chars):
{truncate(html_sample, 8000)}

Identify CSS selectors that extract the data needed for the goal.
Return a JSON object: {{ "field_name": "css_selector", ... }}
"""


def build_structured_extraction_prompt(
    content: str, schema: dict, goal: str
) -> str:
    import json

    return f"""Extract data matching this JSON schema:
{json.dumps(schema, indent=2)}

Goal: {goal}

Content:
{truncate(content, 6000)}

Return only the extracted data as valid JSON matching the schema.
"""
