"""
Scheduler service — APScheduler-based autonomous harvest trigger.

The scheduler reads the current HarvestConfig from disk on every tick,
so changes to harvest_config.json (via PUT /harvest-config) take effect
at the next scheduled run without a restart.

Supported frequencies
─────────────────────
  hourly   → runs at minute 0 of every hour
  daily    → runs at config.schedule.run_time  (HH:MM)
  weekly   → runs every Monday at config.schedule.run_time

Timezone
────────
Accepts any IANA timezone string (e.g. "Asia/Kolkata").
Common abbreviations (IST, UTC, EST) are mapped automatically.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Coroutine

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = structlog.get_logger(__name__)

# ── Timezone abbreviation → IANA name ────────────────────────────────────────
_TZ_MAP: dict[str, str] = {
    "IST": "Asia/Kolkata",
    "UTC": "UTC",
    "GMT": "Europe/London",
    "EST": "America/New_York",
    "CST": "America/Chicago",
    "PST": "America/Los_Angeles",
    "MST": "America/Denver",
    "CET": "Europe/Berlin",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
}

_JOB_ID = "harvest_auto_run"


class SchedulerService:
    """
    Thin wrapper around APScheduler's AsyncIOScheduler.

    Lifecycle — call start() in FastAPI lifespan startup,
                call stop()  in FastAPI lifespan shutdown.
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._running   = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._running:
            self._scheduler.start()
            self._running = True
            logger.info("scheduler_started")

    def stop(self) -> None:
        if self._running:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._running = False
            logger.info("scheduler_stopped")

    # ── Job management ────────────────────────────────────────────────────────

    def schedule_harvest(
        self,
        job_fn:    Callable[[], Coroutine[Any, Any, None]],
        frequency: str,
        run_time:  str,
        timezone:  str,
        enabled:   bool,
    ) -> None:
        """
        Register (or remove) the recurring harvest job.

        Parameters
        ──────────
        job_fn      Async coroutine to execute on each tick.
        frequency   "hourly" | "daily" | "weekly"
        run_time    "HH:MM"  (used for daily / weekly)
        timezone    IANA name or common abbreviation (IST, UTC …)
        enabled     False → remove existing job (disable scheduling)
        """
        tz = _TZ_MAP.get(timezone.upper(), timezone)

        # Remove any existing job first
        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)
            logger.info("scheduler_job_removed")

        if not enabled:
            logger.info("scheduler_disabled")
            return

        # Parse HH:MM
        hour, minute = self._parse_time(run_time)
        trigger: CronTrigger | IntervalTrigger

        if frequency == "hourly":
            trigger = CronTrigger(minute=0, timezone=tz)
        elif frequency == "weekly":
            trigger = CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone=tz)
        else:  # daily (default)
            trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)

        self._scheduler.add_job(
            func    = job_fn,
            trigger = trigger,
            id      = _JOB_ID,
            name    = "LinkedIn Harvest Auto-Run",
            replace_existing = True,
            misfire_grace_time = 300,
        )
        next_run = self._scheduler.get_job(_JOB_ID)
        next_str = str(next_run.next_run_time) if next_run else "unknown"
        logger.info(
            "scheduler_job_registered",
            frequency = frequency,
            run_time  = run_time,
            timezone  = tz,
            next_run  = next_str,
        )

    def get_next_run(self) -> str | None:
        """Return ISO string of next scheduled run, or None."""
        job = self._scheduler.get_job(_JOB_ID)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
        return None

    def is_job_active(self) -> bool:
        """True if a harvest job is currently registered."""
        return self._scheduler.get_job(_JOB_ID) is not None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(run_time: str) -> tuple[int, int]:
        """Parse "HH:MM" → (hour, minute).  Falls back to (9, 0) on error."""
        m = re.match(r"^(\d{1,2}):(\d{2})$", run_time.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
        logger.warning("scheduler_invalid_time", run_time=run_time, fallback="09:00")
        return 9, 0
