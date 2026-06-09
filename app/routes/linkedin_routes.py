"""
LinkedIn Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-linkedin-agent             trigger a LinkedIn harvest run now
  POST   /linkedin-save-session          open browser for manual login + save session
  GET    /linkedin-results               list all saved LinkedIn result files
  GET    /linkedin-results/{run_id}      retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.linkedin_agent import (
    LINKEDIN_SESSION_FILE,
    LinkedInAgent,
    LinkedInLoginError,
    LinkedInScrapedJob,
)
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.response_models import LinkedInJob, LinkedInRunResponse
from app.services.config_service import ConfigService
from app.services.linkedin_storage_service import LinkedInStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LinkedIn Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = LinkedInStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_linkedin_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_linkedin_job(j: LinkedInScrapedJob) -> LinkedInJob:
    return LinkedInJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        work_mode       = j.work_mode,
        company_url     = j.company_url,
        employment_type = j.employment_type,
        source          = "LinkedIn",
    )


def _build_payload(
    run_id:      str,
    executed_at: str,
    f:           FiltersConfig,
    response:    LinkedInRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "LinkedIn",
        "total_found": response.total_found,
        "filters": {
            "keyword":                  f.keyword,
            "location":                 f.location,
            "job_type":                 f.job_type,
            "work_mode":                f.work_mode,
            "search_window_hours":      f.search_window_hours,
            "max_jobs":                 f.max_jobs,
            "domain":                   f.domain,
            "hiring_entity":            f.hiring_entity,
            "gcc_mode":                 f.gcc_mode,
            "salary_min":               f.salary_min,
            "salary_max":               f.salary_max,
            "salary_currency":          f.salary_currency,
            "include_undisclosed_salary": f.include_undisclosed_salary,
        },
        "jobs": [j.model_dump() for j in response.jobs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-linkedin-agent", status_code=status.HTTP_200_OK)
async def run_linkedin_agent() -> Any:
    """
    Trigger a LinkedIn harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("linkedin_search_started", max_jobs=f.max_jobs)

    async def _do_harvest() -> list[LinkedInScrapedJob]:
        agent = LinkedInAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[LinkedInScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except LinkedInLoginError as exc:
        log.error("linkedin_login_failed", error=str(exc))
        return _err("LinkedIn login failed — check credentials or run /linkedin-save-session", str(exc))

    except Exception as exc:
        log.exception("linkedin_harvest_error", error=str(exc))
        return _err("LinkedIn harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("linkedin_jobs_extracted", total=len(scraped))

    jobs = [_to_linkedin_job(j) for j in scraped]

    # ── No results ────────────────────────────────────────────────────────────
    if not jobs:
        response = LinkedInRunResponse(
            run_id      = run_id,
            status      = "no_results",
            source      = "LinkedIn",
            total_found = 0,
            executed_at = now_iso,
            saved_to    = "",
            jobs        = [],
        )
        try:
            payload = _build_payload(run_id, now_iso, f, response)
            response.saved_to = _storage_svc.save_results(payload)
        except Exception:
            pass
        return response.model_dump()

    # ── Success ───────────────────────────────────────────────────────────────
    response = LinkedInRunResponse(
        run_id      = run_id,
        status      = "success",
        source      = "LinkedIn",
        total_found = len(jobs),
        executed_at = now_iso,
        saved_to    = "",
        jobs        = jobs,
    )

    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("linkedin_jobs_saved", count=len(jobs), saved_to=response.saved_to)
        log.info("linkedin_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("linkedin_save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /linkedin-save-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/linkedin-save-session", status_code=status.HTTP_200_OK)
async def save_linkedin_session() -> Any:
    """
    Opens a visible Chromium browser window on your desktop.
    Credentials from .env are auto-filled; complete any OTP/2FA manually.
    The session is saved to data/config/linkedin_session.json and reused
    by all future /run-linkedin-agent calls — no re-login needed.

    Times out after 5 minutes if login is not completed.
    """
    from playwright.async_api import async_playwright

    from app.config import get_settings
    from app.scrapers.browser_manager import (
        _LAUNCH_ARGS,
        _STEALTH_SCRIPTS,
        _USER_AGENT,
    )

    cfg = get_settings()

    async def _do_manual_login() -> str:
        pw      = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False, args=_LAUNCH_ARGS)
        context = await browser.new_context(
            viewport            = {"width": 1366, "height": 900},
            user_agent          = _USER_AGENT,
            locale              = "en-US",
            timezone_id         = "Europe/London",
            color_scheme        = "light",
            java_script_enabled = True,
        )
        for script in _STEALTH_SCRIPTS:
            await context.add_init_script(script)

        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_500)

        _react_fill = """
            ([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                el.focus(); el.click();
                setter.call(el, val);
                ['input', 'change'].forEach(e =>
                    el.dispatchEvent(new Event(e, { bubbles: true, cancelable: true }))
                );
                return el.value === val;
            }
        """
        for sel in ["#username", "input[name='session_key']", "input[type='text']"]:
            try:
                ok = await page.evaluate(_react_fill, [sel, cfg.linkedin_email])
                if ok:
                    break
            except Exception:
                continue

        await page.wait_for_timeout(600)

        for sel in ["#password", "input[name='session_password']", "input[type='password']"]:
            try:
                ok = await page.evaluate(_react_fill, [sel, cfg.linkedin_password])
                if ok:
                    break
            except Exception:
                continue

        await page.wait_for_timeout(600)

        try:
            await page.evaluate("""
                () => {
                    const btn = document.querySelector('button[type="submit"]')
                               || document.querySelector('button.btn__primary--large')
                               || Array.from(document.querySelectorAll('button'))
                                        .find(b => /sign.?in/i.test(b.textContent));
                    if (btn) btn.click();
                }
            """)
        except Exception:
            await page.keyboard.press("Enter")

        logger.info(
            "linkedin_credentials_submitted",
            msg="Credentials auto-filled. If a Microsoft/Google SSO window opened, complete the login there.",
        )

        _GATED      = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")
        _SSO_HOSTS  = ("microsoftonline.com", "login.live.com", "accounts.google.com", "appleid.apple.com")
        for _ in range(150):
            await page.wait_for_timeout(2_000)
            url = page.url
            on_linkedin = "linkedin.com" in url
            on_sso      = any(h in url for h in _SSO_HOSTS)
            # Stay in loop while on SSO provider OR on a LinkedIn gated path
            if on_sso:
                continue
            if on_linkedin and not any(p in url for p in _GATED):
                break
        else:
            await browser.close()
            await pw.stop()
            raise RuntimeError("Login timed out after 5 minutes — please try again")

        LINKEDIN_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(LINKEDIN_SESSION_FILE))
        logger.info("linkedin_session_saved", path=str(LINKEDIN_SESSION_FILE))

        await browser.close()
        await pw.stop()
        return str(LINKEDIN_SESSION_FILE.resolve())

    try:
        if needs_proactor():
            saved_to: str = await run_in_proactor(_do_manual_login)
        else:
            saved_to = await _do_manual_login()
        return {
            "status":   "saved",
            "message":  "LinkedIn session saved — future /run-linkedin-agent calls will skip login",
            "saved_to": saved_to,
        }
    except Exception as exc:
        logger.error("linkedin_save_session_failed", error=str(exc))
        return _err("Failed to save LinkedIn session", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results", status_code=status.HTTP_200_OK)
async def list_linkedin_results() -> Any:
    """List all saved LinkedIn harvest run files, newest first."""
    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_linkedin_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved LinkedIn run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No LinkedIn result found for run_id '{run_id}'",
        )
    return data
