"""
POST /run-linkedin-agent          — real LinkedIn scrape via Playwright form-filling
GET  /run-linkedin-agent/results  — list saved runs   (internal, hidden from Swagger)
GET  /run-linkedin-agent/results/{run_id} — one saved run (internal, hidden from Swagger)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.services.config_service import ConfigService
from app.scrapers.linkedin_form_scraper import (
    LINKEDIN_SESSION_FILE,
    LinkedInBlockedError,
    LinkedInFormScraper,
    LinkedInNoResultsError,
    ScrapedJob,
    _LAUNCH_ARGS,
    _STEALTH,
    _USER_AGENT,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LinkedIn Harvest Agent"])

_OUTPUT_DIR = Path("data/results/linkedin")
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_config_svc = ConfigService()


# ══════════════════════════════════════════════════════════════════════════════
# Response models
# ══════════════════════════════════════════════════════════════════════════════

class JobCard(BaseModel):
    title:     str       = Field(...)
    company:   str       = Field(...)
    location:  str       = Field(...)
    work_mode: str       = Field(default="not_specified")
    source:    str       = Field(default="LinkedIn")
    skills:    list[str] = Field(default_factory=list)
    job_type:  str       = Field(...)
    job_url:   str       = Field(...)
    posted:    str       = Field(...)


class AgentRunResponse(BaseModel):
    run_id:      str           = Field(...)
    executed_at: str           = Field(...)
    status:      str           = Field(...)
    total_found: int           = Field(...)
    keywords:    str           = Field(...)
    jobs:        list[JobCard] = Field(default_factory=list)
    saved_to:    str           = Field(default="")


class RunSummary(BaseModel):
    run_id:      str
    executed_at: str
    keywords:    str
    total_found: int
    status:      str
    file_path:   str


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_run_id(keywords: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keywords} {location}".lower()).strip("_")
    return f"{ts}_{slug[:50]}"


def _scraped_to_card(job: ScrapedJob) -> JobCard:
    return JobCard(
        title     = job.title,
        company   = job.company,
        location  = job.location,
        work_mode = job.work_mode,
        source    = "LinkedIn",
        skills    = [],       # Phase 2 (description fetch) would populate this
        job_type  = job.job_type,
        job_url   = job.job_url,
        posted    = job.posted,
    )


def _save_run(run_id: str, filters: FiltersConfig, response: AgentRunResponse) -> str:
    payload = {
        "run_id":       run_id,
        "executed_at":  response.executed_at,
        "filters": {
            "keyword":             filters.keyword,
            "location":            filters.location,
            "job_type":            filters.job_type,
            "work_mode":           filters.work_mode,
            "search_window_hours": filters.search_window_hours,
            "max_jobs":            filters.max_jobs,
        },
        "result": {
            "status":      response.status,
            "total_found": response.total_found,
            "keywords":    response.keywords,
            "jobs":        [j.model_dump() for j in response.jobs],
        },
    }
    out_file = _OUTPUT_DIR / f"{run_id}.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("run_saved", run_id=run_id, jobs=response.total_found)
    return str(out_file.resolve())


def _load_run(run_id: str) -> dict:
    path = _OUTPUT_DIR / f"{run_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No saved run found with id '{run_id}'",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _list_runs() -> list[RunSummary]:
    summaries: list[RunSummary] = []
    for p in sorted(_OUTPUT_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            summaries.append(RunSummary(
                run_id      = data["run_id"],
                executed_at = data["executed_at"],
                keywords    = data["result"]["keywords"],
                total_found = data["result"]["total_found"],
                status      = data["result"]["status"],
                file_path   = str(p.resolve()),
            ))
        except Exception:
            continue
    return summaries


def _error_json(message: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": message}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=200, content=body)


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/run-linkedin-agent",
    status_code = status.HTTP_200_OK,
)
async def run_linkedin_agent() -> Any:
    """
    Trigger a LinkedIn scrape using the current harvest_config.json settings.
    Edit filters via PUT /harvest-config or the Rule Engine UI.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keywords=f.keyword, location=f.location)
    log.info("agent_start", max_jobs=f.max_jobs)

    async def _do_scrape() -> list[ScrapedJob]:
        async with LinkedInFormScraper() as scraper:
            return await scraper.search(f)

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[ScrapedJob] = await run_in_proactor(_do_scrape)
        else:
            scraped = await _do_scrape()

    except LinkedInBlockedError as exc:
        log.warning("linkedin_blocked", error=str(exc))
        return _error_json(
            "LinkedIn scraping failed",
            "LinkedIn redirected to a login or challenge page — try again later",
        )

    except LinkedInNoResultsError as exc:
        log.info("no_results", error=str(exc))
        response = AgentRunResponse(
            run_id      = run_id,
            executed_at = now_iso,
            status      = "no_results",
            total_found = 0,
            keywords    = f.keyword,
            jobs        = [],
            saved_to    = "",
        )
        try:
            response.saved_to = _save_run(run_id, f, response)
        except Exception:
            pass
        return response.model_dump()

    except TimeoutError as exc:
        log.warning("scrape_timeout", error=str(exc))
        return _error_json(
            "LinkedIn scraping failed",
            "Browser timed out waiting for LinkedIn to respond",
        )

    except Exception as exc:
        err_str = str(exc).lower()
        if any(k in err_str for k in ("browser", "chromium", "playwright", "executable", "process")):
            log.error("browser_error", error=str(exc))
            return _error_json(
                "LinkedIn scraping failed",
                "Chromium browser failed to launch — check Playwright installation",
            )
        log.exception("scrape_error", error=str(exc))
        return _error_json(
            "LinkedIn scraping failed",
            str(exc) or "Unexpected scraping error",
        )

    # ── Build response ─────────────────────────────────────────────────────────
    log.info("agent_done", total=len(scraped))

    response = AgentRunResponse(
        run_id      = run_id,
        executed_at = now_iso,
        status      = "success",
        total_found = len(scraped),
        keywords    = f.keyword,
        jobs        = [_scraped_to_card(j) for j in scraped],
        saved_to    = "",
    )

    # ── Persist to disk ────────────────────────────────────────────────────────
    try:
        response.saved_to = _save_run(run_id, f, response)
    except Exception as exc:
        log.warning("save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# Internal routes — hidden from Swagger
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/run-linkedin-agent/results",
    response_model    = list[RunSummary],
    status_code       = status.HTTP_200_OK,
    include_in_schema = False,
)
async def list_results() -> list[RunSummary]:
    return _list_runs()


@router.get(
    "/run-linkedin-agent/results/{run_id}",
    status_code       = status.HTTP_200_OK,
    include_in_schema = False,
)
async def get_result(run_id: str) -> dict:
    return _load_run(run_id)


# ══════════════════════════════════════════════════════════════════════════════
# POST /linkedin-save-session  — manual login + session persistence
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/linkedin-save-session", status_code=status.HTTP_200_OK)
async def save_linkedin_session() -> Any:
    """
    Opens a visible Chromium browser window on your desktop.
    Log in to LinkedIn manually (including any OTP / 2FA).
    The session cookies are saved to data/config/linkedin_session.json
    and reused by all future /run-linkedin-agent calls — no re-login needed.

    Times out after 5 minutes if login is not completed.
    """
    from playwright.async_api import async_playwright

    async def _do_manual_login() -> str:
        from app.config import get_settings
        cfg = get_settings()

        pw      = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False, args=_LAUNCH_ARGS)
        context = await browser.new_context(
            viewport         = {"width": 1366, "height": 900},
            user_agent       = _USER_AGENT,
            locale           = "en-US",
            timezone_id      = "Europe/London",
            color_scheme     = "light",
            java_script_enabled = True,
        )
        for script in _STEALTH:
            await context.add_init_script(script)

        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_500)

        # Auto-fill credentials from .env so user only needs to handle OTP / 2FA
        react_fill = """
            ([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                el.focus(); el.click();
                setter.call(el, val);
                ['input','change'].forEach(e =>
                    el.dispatchEvent(new Event(e, {bubbles:true, cancelable:true}))
                );
                return el.value === val;
            }
        """
        # Fill email
        for sel in ["#username", "input[name='session_key']", "input[type='text']"]:
            try:
                ok = await page.evaluate(react_fill, [sel, cfg.linkedin_email])
                if ok:
                    break
            except Exception:
                continue

        await page.wait_for_timeout(600)

        # Fill password
        for sel in ["#password", "input[name='session_password']", "input[type='password']"]:
            try:
                ok = await page.evaluate(react_fill, [sel, cfg.linkedin_password])
                if ok:
                    break
            except Exception:
                continue

        await page.wait_for_timeout(600)

        # Click Sign In
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
            msg="Credentials auto-filled. Handle any OTP/verification in the browser window.",
        )

        # Wait up to 5 minutes for successful login (URL leaves all gated paths)
        _GATED = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")
        for _ in range(150):
            await page.wait_for_timeout(2_000)
            url = page.url
            if not any(p in url for p in _GATED):
                break
        else:
            await browser.close()
            await pw.stop()
            raise RuntimeError("Login timed out after 5 minutes — please try again")

        # Save session cookies + localStorage
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
            "status":  "saved",
            "message": "LinkedIn session saved — future /run-linkedin-agent calls will skip login",
            "saved_to": saved_to,
        }
    except Exception as exc:
        logger.error("linkedin_save_session_failed", error=str(exc))
        return _error_json("Failed to save LinkedIn session", str(exc))
