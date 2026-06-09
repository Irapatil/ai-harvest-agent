"""
Configuration — reads from environment variables / .env file.

All settings have safe defaults so the service starts with just ANTHROPIC_API_KEY.
Previously used GEMINI_API_KEY; now replaced with Anthropic Claude API.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Service identity ──────────────────────────────────────────────────────
    service_name:    str     = "job-parser-service"
    service_version: str     = "1.0.0"
    environment:     Literal["development", "staging", "production"] = "development"
    debug:           bool    = False

    # ── Auth ──────────────────────────────────────────────────────────────────
    # Clients must send   X-API-Key: <value>
    # Leave empty to disable auth (not recommended in production)
    api_key: str = ""

    # ── Anthropic Claude ──────────────────────────────────────────────────────
    anthropic_api_key:    str   = ""
    anthropic_model:      str   = "claude-sonnet-4-6"
    anthropic_max_tokens: int   = 2048
    anthropic_timeout_secs: int = 30           # per-request timeout
    anthropic_max_retries:  int = 3            # tenacity retry attempts

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["*"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors(cls, v: str | list[str]) -> list[str]:
        return [o.strip() for o in v.split(",")] if isinstance(v, str) else v

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
