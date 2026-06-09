"""
Run History Service — persistent log of every harvest execution.

History file:  data/results/run_history/run_history.json

Entry schema:
{
  "run_id":        "20260601_143000",
  "sources":       ["naukri", "linkedin"],
  "started_at":    "2026-06-01T14:30:00+00:00",
  "completed_at":  "2026-06-01T14:35:12+00:00",
  "status":        "success",
  "jobs_found":    150,
  "verified_jobs": 0,
  "direct_clients": 60,
  "gcc":           30,
  "staffing_firms": 40,
  "ambiguous":     20
}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_HISTORY_DIR  = Path("data/results/run_history")
_HISTORY_FILE = _HISTORY_DIR / "run_history.json"


class RunHistoryService:

    # ── Write ─────────────────────────────────────────────────────────────────

    def append(self, entry: dict[str, Any]) -> None:
        """Append one run-summary entry to run_history.json."""
        _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history = self._load_raw()
        history.append(entry)
        _HISTORY_FILE.write_text(
            json.dumps(history, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("run_history_appended", run_id=entry.get("run_id"))

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_all(self) -> list[dict]:
        """Return all run history entries, newest first."""
        return list(reversed(self._load_raw()))

    def get(self, run_id: str) -> dict | None:
        for entry in self._load_raw():
            if entry.get("run_id") == run_id:
                return entry
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_raw(self) -> list[dict]:
        if not _HISTORY_FILE.exists():
            return []
        try:
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("run_history_load_error", error=str(exc))
            return []

    @staticmethod
    def make_entry(
        run_id:          str,
        sources:         list[str],
        started_at:      datetime,
        completed_at:    datetime,
        status:          str,
        jobs_found:      int,
        verified_jobs:   int = 0,
        direct_clients:  int = 0,
        gcc:             int = 0,
        staffing_firms:  int = 0,
        ambiguous:       int = 0,
    ) -> dict[str, Any]:
        return {
            "run_id":         run_id,
            "sources":        sources,
            "started_at":     started_at.isoformat(),
            "completed_at":   completed_at.isoformat(),
            "status":         status,
            "jobs_found":     jobs_found,
            "verified_jobs":  verified_jobs,
            "direct_clients": direct_clients,
            "gcc":            gcc,
            "staffing_firms": staffing_firms,
            "ambiguous":      ambiguous,
        }
