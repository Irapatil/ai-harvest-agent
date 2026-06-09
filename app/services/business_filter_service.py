"""
Business Filter Service — post-scraping rule pipeline.

Loads classification rules from data/master/ JSON files at startup.
Applies domain / hiring-entity / GCC classification, then filters.

Pipeline
────────
1. classify_all()   → annotate domain / hiring_entity / is_gcc / job_type
2. apply_all()      → keep only jobs that satisfy active filter rules
3. track_ambiguous()→ append unknown companies to ambiguous_companies.json

Master files (data/master/)
────────────────────────────
domain_keywords.json       – domain → keyword list
gcc_master_list.json       – known GCC company names
staffing_firm_master_list.json – known staffing firm names + generic keywords
direct_client_master_list.json – known direct client company names
ambiguous_companies.json   – append-only log of unclassified companies
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from app.models.harvest_models import FiltersConfig
from app.models.unified_job import UnifiedJob

logger = structlog.get_logger(__name__)

_MASTER_DIR = Path("data/master")


# ── Master-list loader ────────────────────────────────────────────────────────

def _load_json(filename: str, key: str | None = None) -> Any:
    path = _MASTER_DIR / filename
    if not path.exists():
        logger.warning("master_file_missing", file=str(path))
        return [] if key else {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[key] if key else data
    except Exception as exc:
        logger.warning("master_file_load_error", file=str(path), error=str(exc))
        return [] if key else {}


def _load_domain_keywords() -> dict[str, list[str]]:
    raw = _load_json("domain_keywords.json")
    return {k: [s.lower() for s in v] for k, v in raw.items()} if isinstance(raw, dict) else {}


def _load_set(filename: str, key: str = "companies") -> frozenset[str]:
    items = _load_json(filename, key)
    return frozenset(s.lower().strip() for s in items if isinstance(s, str))


# Load once at import time — refreshed by restarting the server
_DOMAIN_KW:          dict[str, list[str]] = _load_domain_keywords()
_KNOWN_GCC:          frozenset[str]       = _load_set("gcc_master_list.json")
_KNOWN_STAFFING:     frozenset[str]       = _load_set("staffing_firm_master_list.json")
_STAFFING_KW:        frozenset[str]       = frozenset(
    s.lower() for s in _load_json("staffing_firm_master_list.json", "keywords") if isinstance(s, str)
)
_KNOWN_DIRECT:       frozenset[str]       = _load_set("direct_client_master_list.json")

# GCC phrase + abbreviation detection
_GCC_PHRASES: frozenset[str] = frozenset({
    "global capability center", "global capability centre",
    "global service center", "global service centre",
    "captive center", "captive centre",
    "center of excellence", "global delivery center",
    "global in-house center", "gic",
})
_GCC_ABBR: frozenset[str] = frozenset({"gcc", "gsc", "coe"})

# Generic staffing single-token keywords
_STAFFING_TOKENS: frozenset[str] = frozenset({
    "staffing", "recruitment", "recruiter", "manpower",
    "placement", "outsourcing", "consulting",
})


def _tok(s: str) -> str:
    return (s or "").lower().strip()


def _tokens(s: str) -> set[str]:
    return set(re.split(r"[\W_]+", _tok(s))) - {""}


# ── Ambiguous company tracker ──────────────────────────────────────────────────

_AMBIGUOUS_FILE = _MASTER_DIR / "ambiguous_companies.json"


def _append_ambiguous(company: str) -> None:
    """Add a company to ambiguous_companies.json (deduplicated, best-effort)."""
    try:
        _MASTER_DIR.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if _AMBIGUOUS_FILE.exists():
            existing = json.loads(_AMBIGUOUS_FILE.read_text(encoding="utf-8"))
        company = company.strip()
        if company and company not in existing:
            existing.append(company)
            _AMBIGUOUS_FILE.write_text(
                json.dumps(sorted(existing), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception as exc:
        logger.debug("ambiguous_append_failed", error=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BusinessFilterService
# ══════════════════════════════════════════════════════════════════════════════

class BusinessFilterService:
    """
    Annotate and filter a list of UnifiedJob instances according to
    the active FiltersConfig rules.
    """

    # ── Public API ─────────────────────────────────────────────────────────────

    def classify_all(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        """Annotate every job with domain / hiring_entity / is_gcc / job_type."""
        for j in jobs:
            if not j.domain or j.domain == "Any":
                j.domain = self._infer_domain(j)
            if not j.hiring_entity or j.hiring_entity == "Any":
                j.is_gcc, j.hiring_entity = self._infer_hiring_entity(j)
            if not j.job_type:
                j.job_type = cfg.job_type
        return jobs

    def apply_all(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        """Return the subset of jobs that satisfy all active filter rules."""
        before = len(jobs)
        jobs   = _deduplicate(jobs)
        jobs   = self._filter_work_mode(jobs, cfg)
        jobs   = self._filter_job_type(jobs, cfg)
        jobs   = self._filter_domain(jobs, cfg)
        jobs   = self._filter_gcc(jobs, cfg)
        jobs   = self._filter_hiring_entity(jobs, cfg)
        jobs   = self._filter_salary(jobs, cfg)
        logger.info("business_filter_complete", before=before, after=len(jobs))
        return jobs

    # ── Domain ─────────────────────────────────────────────────────────────────

    def _infer_domain(self, j: UnifiedJob) -> str:
        text = f"{j.job_title} {j.job_description} {' '.join(j.skills)}".lower()
        scores: dict[str, int] = {}
        for domain, keywords in _DOMAIN_KW.items():
            scores[domain] = sum(1 for kw in keywords if kw in text)
        if not scores:
            return "Non-IT"
        best = max(scores, key=lambda d: scores[d])
        return best if scores[best] > 0 else "Non-IT"

    def _filter_domain(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if cfg.domain == "Any":
            return jobs
        return [j for j in jobs if j.domain == cfg.domain]

    # ── Hiring entity ──────────────────────────────────────────────────────────

    def _infer_hiring_entity(self, j: UnifiedJob) -> tuple[bool, str]:
        """Returns (is_gcc, hiring_entity_label)."""
        c_lower  = _tok(j.company)
        d_lower  = _tok(j.job_description)
        c_tokens = _tokens(j.company)

        # ── GCC: well-known brand match ───────────────────────────────────────
        for known in _KNOWN_GCC:
            if known in c_lower:
                return True, "GCC"

        # ── GCC: phrase match in company name or description ──────────────────
        for phrase in _GCC_PHRASES:
            if phrase in c_lower or phrase in d_lower:
                return True, "GCC"

        # ── GCC: abbreviation token match ─────────────────────────────────────
        if c_tokens & _GCC_ABBR:
            return True, "GCC"

        # ── Staffing: well-known firm match ───────────────────────────────────
        for known in _KNOWN_STAFFING:
            if known in c_lower:
                return False, "Staffing Firm"

        # ── Staffing: generic keyword phrases ─────────────────────────────────
        for kw in _STAFFING_KW:
            if kw in c_lower:
                return False, "Staffing Firm"

        # ── Staffing: single-token match ──────────────────────────────────────
        if c_tokens & _STAFFING_TOKENS:
            return False, "Staffing Firm"

        # ── Direct Client: known brand match ──────────────────────────────────
        for known in _KNOWN_DIRECT:
            if known in c_lower:
                return False, "Direct Client"

        # ── Ambiguous: not in any list ────────────────────────────────────────
        _append_ambiguous(j.company)
        return False, "Ambiguous"

    def _filter_hiring_entity(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if cfg.hiring_entity == "Any":
            return jobs
        return [j for j in jobs if j.hiring_entity == cfg.hiring_entity]

    # ── GCC mode ───────────────────────────────────────────────────────────────

    def _filter_gcc(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if cfg.gcc_mode == "include_gcc":
            return jobs
        if cfg.gcc_mode == "gcc_only":
            return [j for j in jobs if j.is_gcc]
        return [j for j in jobs if not j.is_gcc]   # exclude_gcc

    # ── Work mode ──────────────────────────────────────────────────────────────

    def _filter_work_mode(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if cfg.work_mode == "Any":
            return jobs
        target = cfg.work_mode.lower()
        return [
            j for j in jobs
            if j.work_mode.lower() == target or j.work_mode == "not_specified"
        ]

    # ── Job type ───────────────────────────────────────────────────────────────

    _JT_KEYWORDS: dict[str, list[str]] = {
        "contract":   ["contract", "c2c", "1099", "temp", "contractor", "temporary"],
        "freelance":  ["freelance", "independent", "self-employed", "gig"],
        "part-time":  ["part-time", "part time", "parttime"],
        "permanent":  ["permanent", "regular", "full-time", "fulltime", "full time"],
        "full-time":  ["permanent", "regular", "full-time", "fulltime", "full time"],
    }

    def _filter_job_type(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if cfg.job_type == "Any":
            return jobs
        target_kws = self._JT_KEYWORDS.get(cfg.job_type.lower(), [])
        result = []
        for j in jobs:
            if j.job_type.lower() == cfg.job_type.lower():
                result.append(j)
            elif target_kws and any(kw in f"{j.job_title} {j.job_description}".lower() for kw in target_kws):
                j.job_type = cfg.job_type
                result.append(j)
            elif not j.job_type:
                j.job_type = cfg.job_type
                result.append(j)
        return result

    # ── Salary ─────────────────────────────────────────────────────────────────

    def _filter_salary(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        if not cfg.salary_min and not cfg.salary_max:
            return jobs
        result = []
        for j in jobs:
            parsed = _parse_salary_lpa(j.salary)
            if parsed is None:
                if cfg.include_undisclosed_salary:
                    result.append(j)
                continue
            mid_lpa = (parsed[0] + parsed[1]) / 2
            min_lpa = (cfg.salary_min / 100_000) if cfg.salary_min else None
            max_lpa = (cfg.salary_max / 100_000) if cfg.salary_max else None
            if min_lpa and mid_lpa < min_lpa:
                continue
            if max_lpa and mid_lpa > max_lpa:
                continue
            result.append(j)
        return result


# ── Deduplication ─────────────────────────────────────────────────────────────

def _deduplicate(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """
    Remove duplicate jobs using three signals (all three checked):
    1. Exact job_url match
    2. Numeric job ID extracted from URL
    3. Normalised company + title combination
    """
    seen_urls: set[str]       = set()
    seen_ids:  set[str]       = set()
    seen_ct:   set[str]       = set()
    result:    list[UnifiedJob] = []

    _id_re = re.compile(r"[/-](\d{7,})")

    for j in jobs:
        url = (j.job_url or "").split("?")[0].rstrip("/").lower()
        jid = m.group(1) if (m := _id_re.search(url)) else ""
        ct  = re.sub(r"\s+", " ", f"{j.company} {j.job_title}".lower().strip())

        if url and url in seen_urls:
            continue
        if jid and jid in seen_ids:
            continue
        if ct and ct in seen_ct:
            continue

        if url:
            seen_urls.add(url)
        if jid:
            seen_ids.add(jid)
        if ct:
            seen_ct.add(ct)
        result.append(j)

    removed = len(jobs) - len(result)
    if removed:
        logger.info("deduplication_complete", removed=removed, kept=len(result))
    return result


# ── Salary parser ──────────────────────────────────────────────────────────────

_LPA_RANGE_RE  = re.compile(r"([\d.]+)\s*[-–to]+\s*([\d.]+)\s*(?:lpa|lacs?|lakhs?|l\b)", re.I)
_LPA_SINGLE_RE = re.compile(r"([\d.]+)\s*(?:lpa|lacs?|lakhs?|l\b)", re.I)
_NOT_DISCLOSED = {"not disclosed", "not specified", "n/a", "na", ""}


def _parse_salary_lpa(raw: str) -> tuple[float, float] | None:
    if not raw or raw.strip().lower() in _NOT_DISCLOSED:
        return None
    m = _LPA_RANGE_RE.search(raw)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _LPA_SINGLE_RE.search(raw)
    if m:
        v = float(m.group(1))
        return v, v
    return None
