"""Main HarvestAgent: drives Claude + Playwright in an agentic loop."""
from __future__ import annotations

import json
from typing import Any

import structlog

from app.agents.base_agent import BaseAgent
from app.models.agent import AgentConfig, AgentType
from app.prompts.harvest_prompts import HARVEST_SYSTEM_PROMPT, build_harvest_user_prompt
from app.services.llm_service import HARVEST_TOOLS, LLMMessage, LLMService
from app.services.playwright_service import PageSnapshot, PlaywrightService

logger = structlog.get_logger(__name__)


class HarvestAgent(BaseAgent):
    """
    Agentic harvester that lets Claude decide which actions to take.

    Loop:
        1. Send current page snapshot + goal to Claude (with tools)
        2. Claude calls a tool (navigate / click / extract_data / scroll / finish)
        3. Execute the tool and feed result back
        4. Repeat until Claude calls `finish` or max_iterations reached
    """

    agent_type = AgentType.HARVEST

    def __init__(
        self,
        llm_service: LLMService,
        playwright_service: PlaywrightService,
        config: AgentConfig | None = None,
    ) -> None:
        super().__init__(llm_service, playwright_service, config)
        self._current_snapshot: PageSnapshot | None = None
        self._collected: list[dict[str, Any]] = []

    # ── BaseAgent interface ───────────────────────────────────────────────────────

    async def plan(self, url: str, goal: str) -> dict[str, Any]:
        """Ask Claude to outline a navigation strategy."""
        prompt = (
            f"You are about to harvest data from the web.\n"
            f"Starting URL: {url}\n"
            f"Goal: {goal}\n\n"
            f"Briefly describe your strategy: which pages to visit, "
            f"what signals to look for, and how you will extract the data."
        )
        strategy = await self._llm.complete_text(prompt, system=HARVEST_SYSTEM_PROMPT)
        return {"strategy": strategy, "start_url": url, "goal": goal}

    async def execute(
        self, url: str, goal: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the tool-use agentic loop."""
        messages: list[dict[str, Any]] = [
            LLMMessage.user(build_harvest_user_prompt(url, goal, plan["strategy"]))
        ]
        final_result: dict[str, Any] = {}

        for iteration in range(self._config.max_iterations):
            response = await self._llm.complete(
                messages=messages,
                system=HARVEST_SYSTEM_PROMPT,
                tools=HARVEST_TOOLS,
            )

            tool_call = self._llm.get_tool_use(response)
            if tool_call is None:
                # Claude produced a text response without a tool call → done
                logger.info("no_tool_call", iteration=iteration)
                break

            tool_id, tool_name, tool_input = tool_call
            logger.info("tool_called", tool=tool_name, iteration=iteration)
            self._record_step(tool_name, tool_input)

            # ── Dispatch tool call ────────────────────────────────────────────────
            if tool_name == "finish":
                final_result = tool_input.get("data", {})
                final_result["_summary"] = tool_input.get("summary", "")
                final_result["_pages_visited"] = tool_input.get("pages_visited", [])
                messages.append({"role": "assistant", "content": response.content})
                messages.append(LLMMessage.tool_result(tool_id, "Harvest complete."))
                break

            observation = await self._dispatch_tool(tool_name, tool_input)

            # Append assistant message + tool result
            messages.append({"role": "assistant", "content": response.content})
            messages.append(LLMMessage.tool_result(tool_id, observation))

        return final_result or {"_raw_collected": self._collected}

    async def reflect(
        self, raw_results: dict[str, Any], goal: str
    ) -> dict[str, Any]:
        """Have Claude review and clean the harvested data."""
        if not raw_results:
            return raw_results

        prompt = (
            f"Review this harvested data for goal: '{goal}'\n\n"
            f"Data:\n{json.dumps(raw_results, indent=2, default=str)[:8000]}\n\n"
            f"Clean, deduplicate, and structure it. Return valid JSON only."
        )
        try:
            cleaned = await self._llm.extract_json(
                content=json.dumps(raw_results, default=str),
                schema_description=f"Structured data for goal: {goal}",
            )
            return cleaned
        except Exception:
            return raw_results

    # ── Tool dispatch ─────────────────────────────────────────────────────────────

    async def _dispatch_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        try:
            if tool_name == "navigate":
                url = tool_input["url"]
                wait = tool_input.get("wait_until", "networkidle")
                self._current_snapshot = await self._playwright.navigate(url, wait_until=wait)
                self._visited(url)
                return self._snapshot_summary(self._current_snapshot)

            elif tool_name == "click":
                if self._current_snapshot is None:
                    return "Error: no page loaded yet."
                selector = tool_input["selector"]
                self._current_snapshot = await self._playwright.click_and_snapshot(
                    self._current_snapshot.url, selector
                )
                self._visited(self._current_snapshot.url)
                return self._snapshot_summary(self._current_snapshot)

            elif tool_name == "extract_data":
                if self._current_snapshot is None:
                    return "Error: no page loaded yet."
                instructions = tool_input.get("instructions", "Extract all relevant data.")
                schema_desc = json.dumps(tool_input.get("schema", {}))
                extracted = await self._llm.extract_json(
                    content=self._current_snapshot.text,
                    schema_description=f"{instructions}\nSchema: {schema_desc}",
                )
                self._collected.append(extracted)
                return f"Extracted: {json.dumps(extracted, default=str)[:500]}"

            elif tool_name == "scroll":
                if self._current_snapshot is None:
                    return "Error: no page loaded yet."
                count = tool_input.get("count", 3)
                self._current_snapshot = await self._playwright.scroll_and_snapshot(
                    self._current_snapshot.url, scroll_count=count
                )
                return self._snapshot_summary(self._current_snapshot)

            elif tool_name == "fill_form":
                if self._current_snapshot is None:
                    return "Error: no page loaded yet."
                self._current_snapshot = await self._playwright.fill_and_submit(
                    self._current_snapshot.url,
                    fields=tool_input["fields"],
                    submit_selector=tool_input["submit_selector"],
                )
                return self._snapshot_summary(self._current_snapshot)

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as exc:
            logger.warning("tool_error", tool=tool_name, error=str(exc))
            return f"Tool error: {exc}"

    @staticmethod
    def _snapshot_summary(snap: PageSnapshot) -> str:
        links_preview = "; ".join(
            f"{l['text'][:30]} → {l['href'][:60]}" for l in snap.links[:5]
        )
        return (
            f"URL: {snap.url}\n"
            f"Title: {snap.title}\n"
            f"Text preview: {snap.text[:500]}\n"
            f"Links ({len(snap.links)} total): {links_preview}\n"
            f"Forms: {len(snap.forms)}"
        )
