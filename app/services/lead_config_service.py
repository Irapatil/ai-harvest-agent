"""
Lead Intelligence Config Service — loads/saves lead_intelligence_config.json.

Contract
────────
• Returns a plain dict — no Pydantic model, so callers can access any key directly.
• Missing keys fall back to defaults — never raises on partial configs.
• Config file path: data/config/lead_intelligence_config.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_CONFIG_PATH = Path("data/config/lead_intelligence_config.json")

_DEFAULTS: dict[str, Any] = {
    "search_sources":             ["linkedin", "premium_naukri"],
    "search_posts":               True,
    "fallback_to_premium_naukri": True,
    "linkedin": {
        "keywords":    ["hiring", "we are hiring", "looking for", "open position"],
        "max_posts":   50,
        "max_pages":   5,
        "scroll_times": 8,
    },
    "premium_naukri": {
        "search_by_name":         True,
        "search_by_company":      True,
        "use_previous_companies": True,
        "max_profiles":           3,
        "open_profile_tabs":      True,
    },
    "validate_email":      True,
    "validate_phone":      True,
    "minimum_confidence":  0.70,
    "output": {
        "json":      True,
        "excel":     True,
        "crm_ready": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts."""
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class LeadConfigService:

    def load(self) -> dict[str, Any]:
        """Load config, merging with defaults so missing keys always have a value."""
        if not _CONFIG_PATH.exists():
            logger.warning(
                "lead_config_not_found",
                path=str(_CONFIG_PATH),
                note="Using built-in defaults.",
            )
            return _DEFAULTS.copy()
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            merged = _deep_merge(_DEFAULTS, raw)
            logger.info("lead_config_loaded", path=str(_CONFIG_PATH))
            return merged
        except Exception as exc:
            logger.warning("lead_config_load_error", error=str(exc))
            return _DEFAULTS.copy()

    def save(self, cfg: dict[str, Any]) -> None:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("lead_config_saved", path=str(_CONFIG_PATH))

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)
