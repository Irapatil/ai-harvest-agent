"""Anthropic Claude wrapper with tool-use support."""
from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.core.exceptions import LLMError

logger = structlog.get_logger(__name__)


# ── Tool definitions the LLM can call ────────────────────────────────────────────

HARVEST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a URL and get the page content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "networkidle", "domcontentloaded"],
                    "description": "When to consider navigation complete",
                    "default": "networkidle",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an element on the current page by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to click"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "extract_data",
        "description": "Extract structured data from the current page content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema": {
                    "type": "object",
                    "description": "JSON schema describing the data to extract",
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural language extraction instructions",
                },
            },
            "required": ["instructions"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page to load more content (infinite scroll).",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of scroll actions", "default": 3},
            },
        },
    },
    {
        "name": "fill_form",
        "description": "Fill a form and submit it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Mapping of CSS selector → value",
                },
                "submit_selector": {
                    "type": "string",
                    "description": "CSS selector of the submit button",
                },
            },
            "required": ["fields", "submit_selector"],
        },
    },
    {
        "name": "finish",
        "description": "Signal that harvesting is complete and return the final structured result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "description": "The final harvested and structured data",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was harvested",
                },
                "pages_visited": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs visited",
                },
            },
            "required": ["data", "summary"],
        },
    },
]


class LLMMessage:
    """Helper to build messages list."""

    @staticmethod
    def user(content: str) -> dict[str, Any]:
        return {"role": "user", "content": content}

    @staticmethod
    def assistant(content: str) -> dict[str, Any]:
        return {"role": "assistant", "content": content}

    @staticmethod
    def tool_result(tool_use_id: str, content: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        }


class LLMService:
    """Anthropic Claude client with retry logic and tool-use support."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens
        self._temperature = settings.anthropic_temperature

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> anthropic.types.Message:
        """Call Claude and return the raw Message."""
        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools
            response = await self._client.messages.create(**kwargs)
            logger.debug(
                "llm_response",
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

    async def complete_text(self, prompt: str, system: str = "") -> str:
        """Convenience wrapper returning plain text."""
        response = await self.complete(
            messages=[LLMMessage.user(prompt)],
            system=system,
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    async def extract_json(
        self,
        content: str,
        schema_description: str,
        system: str = "",
    ) -> dict[str, Any]:
        """Ask Claude to extract structured JSON from arbitrary content."""
        prompt = (
            f"Extract the following data from the content below.\n\n"
            f"Schema: {schema_description}\n\n"
            f"Content:\n{content}\n\n"
            f"Return only valid JSON, no explanation."
        )
        text = await self.complete_text(prompt, system=system)
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid JSON: {exc}") from exc

    def get_tool_use(
        self, response: anthropic.types.Message
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Return (tool_use_id, tool_name, tool_input) if the model wants to use a tool."""
        for block in response.content:
            if block.type == "tool_use":
                return block.id, block.name, block.input  # type: ignore[union-attr]
        return None

    def get_text(self, response: anthropic.types.Message) -> str:
        """Extract plain text from a response."""
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""
