"""Persist harvest results to disk (local) or S3."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)


class StorageService:
    """Save screenshots and extracted JSON to the configured storage backend."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._local_dir = Path(settings.storage_local_dir)

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    # ── Screenshots ──────────────────────────────────────────────────────────────

    async def save_screenshot(self, job_id: str, page_number: int, b64_data: str) -> str:
        """Save a base64-encoded screenshot and return the file path."""
        if self._settings.storage_backend == "s3":
            return await self._upload_screenshot_s3(job_id, page_number, b64_data)
        return self._save_screenshot_local(job_id, page_number, b64_data)

    def _save_screenshot_local(self, job_id: str, page_number: int, b64_data: str) -> str:
        dir_path = self._local_dir / job_id / "screenshots"
        self._ensure_dir(dir_path)
        filename = f"page_{page_number:04d}.png"
        file_path = dir_path / filename
        file_path.write_bytes(base64.b64decode(b64_data))
        logger.debug("screenshot_saved", path=str(file_path))
        return str(file_path)

    async def _upload_screenshot_s3(self, job_id: str, page_number: int, b64_data: str) -> str:
        # Placeholder — wire up boto3 / aioboto3 as needed
        raise NotImplementedError("S3 backend not yet implemented")

    # ── JSON Results ─────────────────────────────────────────────────────────────

    async def save_result(self, job_id: str, data: dict[str, Any]) -> str:
        """Save extracted data as JSON and return the path."""
        if self._settings.storage_backend == "s3":
            return await self._upload_result_s3(job_id, data)
        return self._save_result_local(job_id, data)

    def _save_result_local(self, job_id: str, data: dict[str, Any]) -> str:
        dir_path = self._local_dir / job_id
        self._ensure_dir(dir_path)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        file_path = dir_path / f"result_{ts}.json"
        file_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        logger.info("result_json_saved", path=str(file_path))
        return str(file_path)

    async def _upload_result_s3(self, job_id: str, data: dict[str, Any]) -> str:
        raise NotImplementedError("S3 backend not yet implemented")

    # ── Read Back ────────────────────────────────────────────────────────────────

    def list_job_files(self, job_id: str) -> list[str]:
        job_dir = self._local_dir / job_id
        if not job_dir.exists():
            return []
        return [str(p) for p in job_dir.rglob("*") if p.is_file()]
