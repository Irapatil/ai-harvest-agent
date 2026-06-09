"""
Config service — load and save harvest_config.json.

The config file lives at  data/config/harvest_config.json.
If the file does not exist, default values from HarvestConfig are returned
and nothing is written until an explicit save is requested.
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog

from app.models.harvest_models import HarvestConfig

logger = structlog.get_logger(__name__)

_CONFIG_PATH = Path("data/config/harvest_config.json")


class ConfigService:
    """Load and persist the agent's harvest configuration."""

    # ── Read ──────────────────────────────────────────────────────────────────

    def load(self) -> HarvestConfig:
        """
        Read harvest_config.json and return a validated HarvestConfig.
        Falls back to default values when the file is missing or malformed.
        """
        if not _CONFIG_PATH.exists():
            logger.warning("config_not_found", path=str(_CONFIG_PATH), using="defaults")
            return HarvestConfig()
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            config = HarvestConfig(**raw)
            logger.info("config_loaded", path=str(_CONFIG_PATH))
            return config
        except Exception as exc:
            logger.error("config_load_error", path=str(_CONFIG_PATH), error=str(exc))
            return HarvestConfig()

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(self, config: HarvestConfig) -> None:
        """Persist a HarvestConfig to harvest_config.json (creates directories)."""
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("config_saved", path=str(_CONFIG_PATH))
