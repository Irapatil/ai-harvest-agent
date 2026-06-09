"""
Harvest storage service — persist job results and list previous runs.

Every successful harvest run is saved as a timestamped JSON file under:

    data/results/linkedin/
        <YYYYMMDD_HHMMSS>_<slug>.json

The file name embeds keyword + location so runs are human-readable
at a glance in the directory listing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.models.response_models import ResultFileSummary

logger = structlog.get_logger(__name__)

_BASE_PATH = Path("data/results/linkedin")


class HarvestStorageService:
    """Save and retrieve LinkedIn harvest run results."""

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_results(self, data: dict[str, Any]) -> str:
        """
        Persist *data* as a JSON file.

        Parameters
        ──────────
        data    Full run payload (run_id, executed_at, status, filters, jobs …)

        Returns
        ───────
        Absolute path of the saved file as a string.
        """
        _BASE_PATH.mkdir(parents=True, exist_ok=True)

        # Build a readable filename from run metadata
        run_id: str = data.get("run_id", self._timestamp_slug("", ""))
        filename    = f"{run_id}.json"
        file_path   = _BASE_PATH / filename

        file_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("harvest_saved", path=str(file_path), jobs=data.get("total_found", 0))
        return str(file_path.resolve())

    # ── Read — list ───────────────────────────────────────────────────────────

    def list_results(self) -> list[ResultFileSummary]:
        """Return summaries of all saved runs, newest first."""
        if not _BASE_PATH.exists():
            return []

        summaries: list[ResultFileSummary] = []
        for p in sorted(_BASE_PATH.glob("*.json"), reverse=True):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                filters = raw.get("filters", {})
                # Support both new format (status at root) and legacy format (nested under result{})
                result_block = raw.get("result", {})
                summaries.append(
                    ResultFileSummary(
                        run_id      = raw.get("run_id", p.stem),
                        executed_at = raw.get("executed_at", ""),
                        status      = raw.get("status") or result_block.get("status", "unknown"),
                        total_found = raw.get("total_found") or result_block.get("total_found", 0),
                        keyword     = filters.get("keyword") or result_block.get("keywords", ""),
                        location    = filters.get("location", ""),
                        file_path   = str(p.resolve()),
                    )
                )
            except Exception as exc:
                logger.debug("result_file_skip", path=str(p), error=str(exc))
                continue
        return summaries

    # ── Read — single ─────────────────────────────────────────────────────────

    def get_result(self, run_id: str) -> dict | None:
        """Load one saved run by its run_id. Returns None if not found."""
        path = _BASE_PATH / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("result_load_error", run_id=run_id, error=str(exc))
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _timestamp_slug(keyword: str, location: str) -> str:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
        return f"{ts}_{slug[:40]}"
