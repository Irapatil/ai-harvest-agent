"""Dice storage service — persist job results and list previous runs.

Every successful harvest run is saved as a timestamped JSON file under:
    data/results/dice/
        YYYYMMDD_HHMMSS_dice.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_BASE_PATH = Path("data/results/dice")


class DiceStorageService:
    """Save and retrieve Dice harvest run results."""

    def save_results(self, data: dict[str, Any]) -> str:
        _BASE_PATH.mkdir(parents=True, exist_ok=True)
        ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename  = f"{ts}_dice.json"
        file_path = _BASE_PATH / filename
        file_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("dice_results_saved", path=str(file_path), jobs=data.get("total_found", 0))
        return str(file_path.resolve())

    def list_results(self) -> list[dict]:
        if not _BASE_PATH.exists():
            return []
        results: list[dict] = []
        for p in sorted(_BASE_PATH.glob("*_dice.json"), reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                results.append({
                    "run_id":      data.get("run_id", p.stem),
                    "executed_at": data.get("executed_at", ""),
                    "status":      data.get("status", "unknown"),
                    "total_found": data.get("total_found", 0),
                    "source":      data.get("source", "Dice"),
                    "file_path":   str(p.resolve()),
                })
            except Exception as exc:
                logger.debug("dice_result_file_skip", path=str(p), error=str(exc))
                continue
        return results

    def get_result(self, run_id: str) -> dict | None:
        if not _BASE_PATH.exists():
            return None
        for p in _BASE_PATH.glob("*_dice.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("run_id") == run_id:
                    return data
            except Exception:
                continue
        return None
