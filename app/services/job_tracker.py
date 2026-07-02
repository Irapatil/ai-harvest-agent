"""
Job Tracker — process-level in-memory status store for background harvest runs.

Every POST /run-harvest-agent creates one entry here.
The background coroutine updates it at key milestones.
GET /harvest-status/{job_id} reads from it.

State is persisted to disk after every write so the frontend can poll
even after a hot-reload (though in-flight jobs are marked "failed" on
restart because their background tasks don't survive process termination).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TRACKER_FILE = Path("data/results/run_history/job_tracker.json")


@dataclass
class JobStatus:
    job_id:       str
    run_id:       str = ""
    status:       str = "running"   # running | success | no_results | failed
    progress:     int = 0           # 0–100
    message:      str = "Harvest started"
    linkedin:     int = 0
    naukri:       int = 0
    dice:         int = 0
    combined:     int = 0
    started_at:   str = ""
    completed_at: str = ""
    excel_path:   str = ""
    json_path:    str = ""
    error:        str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobTracker:
    """Process-level keyed store — no external dependencies, no locks needed
    because FastAPI runs a single-threaded async event loop."""

    _jobs: dict[str, JobStatus] = {}

    # ── Write ─────────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, job_id: str, run_id: str) -> JobStatus:
        js = JobStatus(
            job_id     = job_id,
            run_id     = run_id,
            started_at = datetime.now(timezone.utc).isoformat(),
        )
        cls._jobs[job_id] = js
        cls._persist()
        return js

    @classmethod
    def update(cls, job_id: str, **kwargs: Any) -> None:
        js = cls._jobs.get(job_id)
        if js is None:
            return
        for k, v in kwargs.items():
            if hasattr(js, k):
                setattr(js, k, v)
        cls._persist()

    # ── Read ──────────────────────────────────────────────────────────────────

    @classmethod
    def get(cls, job_id: str) -> JobStatus | None:
        return cls._jobs.get(job_id)

    # ── Startup restore ───────────────────────────────────────────────────────

    @classmethod
    def load_from_disk(cls) -> None:
        """
        Called once at application startup.
        Any job still in 'running' state didn't survive the restart — mark failed.
        """
        try:
            if not _TRACKER_FILE.exists():
                return
            data = json.loads(_TRACKER_FILE.read_text(encoding="utf-8"))
            for job_id, d in data.items():
                js = JobStatus(**{k: v for k, v in d.items() if k in JobStatus.__dataclass_fields__})
                if js.status == "running":
                    js.status  = "failed"
                    js.message = "Server restarted during harvest"
                    js.error   = "Server restarted during harvest"
                cls._jobs[job_id] = js
        except Exception:
            pass

    # ── Internal ──────────────────────────────────────────────────────────────

    @classmethod
    def _persist(cls) -> None:
        try:
            _TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TRACKER_FILE.write_text(
                json.dumps({k: v.to_dict() for k, v in cls._jobs.items()}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
