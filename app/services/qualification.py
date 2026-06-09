"""
Hard qualification rules engine for job postings.

Usage
─────
    from app.services.qualification import apply_qualification_rules, load_rules

    rules  = load_rules("config/qualify.json")
    result = apply_qualification_rules(enriched_job, rules)

    if not result.passed:
        for v in result.violations:
            print(f"FAIL  [{v.rule}] {v.reason}")

Each top-level key in the config dict activates one rule group.
Omitting a key skips that group entirely — only declare the constraints
you care about.

Config reference
────────────────
{
  "employment_type": {
    "allowed":              ["contract", "freelance"],
    "reject_not_specified": true          // default true
  },

  "work_mode": {
    "allowed":              ["remote", "hybrid"],
    "reject_not_specified": false         // default false
  },

  "location": {
    "must_contain_any":  ["London", "UK", "United Kingdom", "Remote"],
    "case_sensitive":    false,           // default false
    "skip_if_remote":    true             // default true — bypass when work_mode=remote
  },

  "salary": {
    "min_value":         400,             // numeric lower bound
    "period":            "daily",         // period that min_value is expressed in
    "currency":          "GBP",           // required currency (null/omit = any)
    "reject_if_missing": false            // default false
  },

  "skills": {
    "must_have_any":     ["Java", "Python", "Go"],   // at least one required
    "must_have_all":     ["Docker"],                  // every one required
    "must_not_have_any": ["COBOL", "VBA"],            // hard blocklist
    "search_in":         "all"            // "required" | "preferred" | "all" (default)
  },

  "experience": {
    "max_years_required": 15,   // reject when JD demands MORE than this
    "min_years_required":  3    // reject when JD demands FEWER than this
  },

  "title": {
    "must_contain_any":    ["Developer", "Engineer", "Architect"],
    "must_not_contain_any": ["Junior", "Graduate", "Intern"],
    "case_sensitive":       false
  },

  "confidence": {
    "min_score": 0.5            // skip low-confidence Gemini extractions
  }
}

Skill matching
──────────────
Terms use whole-word regex boundaries (re.IGNORECASE) so "Java" matches
"Java 17" and "Core Java" but NOT "JavaScript".  Multi-word terms such as
"Spring Boot" are matched as a phrase.

Salary normalisation
────────────────────
All amounts are converted to an annual equivalent for comparison using
standard contractor working assumptions:
    hourly  × 2080   (8 h/day × 260 working days)
    daily   × 260
    weekly  × 52
    monthly × 12
    annual  × 1

Currency must match (GBP ≠ USD); if currencies differ the salary rule is
treated as missing data and follows the `on_missing_data` policy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.models.job_parser import (
    EmploymentType,
    ParsedJobDescription,
    SalaryPeriod,
    WorkMode,
)
from app.models.linkedin import EnrichedLinkedInJob


# ══════════════════════════════════════════════════════════════════════════════
# Custom error
# ══════════════════════════════════════════════════════════════════════════════

class RulesConfigError(ValueError):
    """Raised when the rules config dict has an invalid structure or value."""


# ══════════════════════════════════════════════════════════════════════════════
# Result types
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RuleVerdict:
    """Outcome of evaluating one rule group against one job."""

    rule:    str    # e.g. "employment_type", "salary", "skills"
    passed:  bool
    reason:  str    # always set — explains the decision in plain English
    details: dict   = field(default_factory=dict, hash=False, compare=False)

    def __str__(self) -> str:
        icon = "✓" if self.passed else "✗"
        return f"{icon} [{self.rule}] {self.reason}"


@dataclass
class QualificationResult:
    """
    Aggregate outcome of all configured rule groups for one job.

    Attributes
    ----------
    passed      True iff every applied rule verdict is True.
    job_id      Taken from EnrichedLinkedInJob.job_id (None for bare parse).
    job_title   Taken from the parsed title or card title.
    verdicts    All rule verdicts in evaluation order.
    violations  Convenience view: verdicts where passed=False.
    passes      Convenience view: verdicts where passed=True.
    score       Fraction of rules that passed (0.0–1.0).
    """

    passed:     bool
    job_id:     str | None
    job_title:  str | None
    verdicts:   list[RuleVerdict]

    # ── Derived views ─────────────────────────────────────────────────────────

    @property
    def violations(self) -> list[RuleVerdict]:
        return [v for v in self.verdicts if not v.passed]

    @property
    def passes(self) -> list[RuleVerdict]:
        return [v for v in self.verdicts if v.passed]

    @property
    def score(self) -> float:
        if not self.verdicts:
            return 1.0
        return sum(1 for v in self.verdicts if v.passed) / len(self.verdicts)

    def as_dict(self) -> dict:
        """Serialisable summary — safe to JSON-dump or log."""
        return {
            "passed":     self.passed,
            "job_id":     self.job_id,
            "job_title":  self.job_title,
            "score":      round(self.score, 3),
            "violations": [
                {"rule": v.rule, "reason": v.reason, "details": v.details}
                for v in self.violations
            ],
            "verdicts": [
                {"rule": v.rule, "passed": v.passed, "reason": v.reason}
                for v in self.verdicts
            ],
        }

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"QualificationResult({status}, score={self.score:.2f}, "
            f"rules={len(self.verdicts)}, violations={len(self.violations)})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

# Multipliers to convert any SalaryPeriod to an annual equivalent.
_TO_ANNUAL: dict[str, float] = {
    SalaryPeriod.HOURLY:  2_080.0,   # 8 h × 260 working days
    SalaryPeriod.DAILY:     260.0,
    SalaryPeriod.WEEKLY:     52.0,
    SalaryPeriod.MONTHLY:    12.0,
    SalaryPeriod.ANNUAL:      1.0,
}


def _to_annual(value: float, period: str) -> float:
    """Normalise *value* to an annual equivalent amount."""
    multiplier = _TO_ANNUAL.get(period)
    if multiplier is None:
        raise RulesConfigError(
            f"Unknown salary period '{period}'. "
            f"Valid values: {list(_TO_ANNUAL)}"
        )
    return value * multiplier


def _skill_matches(term: str, skill: str) -> bool:
    """
    Case-insensitive whole-word match of *term* against one *skill* string.

    'Java'       matches 'Java 17'    → True
    'Java'       matches 'JavaScript' → False   (word boundary prevents it)
    'Spring Boot' matches 'Spring Boot 3' → True
    """
    pattern = rf"\b{re.escape(term)}\b"
    return bool(re.search(pattern, skill, re.IGNORECASE))


def _any_skill_matches(term: str, skill_list: list[str]) -> bool:
    """Return True if *term* matches ANY string in *skill_list*."""
    return any(_skill_matches(term, s) for s in skill_list)


def _missing_verdict(rule: str, field_name: str, policy: Literal["skip", "fail"]) -> RuleVerdict:
    """Produce a consistent verdict for absent fields under the missing-data policy."""
    passed = policy == "skip"
    reason = (
        f"'{field_name}' is not available in this job's parsed data — "
        f"{'skipped (permissive)' if passed else 'rejected (strict)'}"
    )
    return RuleVerdict(rule=rule, passed=passed, reason=reason)


def _require_list(cfg: dict, key: str, rule: str) -> list[str]:
    """Pull a list[str] from *cfg[key]*, raising RulesConfigError on bad types."""
    val = cfg.get(key, [])
    if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
        raise RulesConfigError(
            f"Rule '{rule}.{key}' must be a list of strings, got {val!r}"
        )
    return [s.strip() for s in val if s.strip()]


def _require_bool(cfg: dict, key: str, rule: str, default: bool) -> bool:
    val = cfg.get(key, default)
    if not isinstance(val, bool):
        raise RulesConfigError(
            f"Rule '{rule}.{key}' must be a boolean, got {val!r}"
        )
    return val


# ══════════════════════════════════════════════════════════════════════════════
# Individual rule checkers
# ══════════════════════════════════════════════════════════════════════════════

def _check_employment_type(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "employment_type"

    allowed              = _require_list(cfg, "allowed", RULE)
    reject_not_specified = _require_bool(cfg, "reject_not_specified", RULE, default=True)

    if not allowed and not reject_not_specified:
        return RuleVerdict(rule=RULE, passed=True, reason="No constraints configured")

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    emp = parsed.employment_type
    actual = emp.value if hasattr(emp, "value") else str(emp)

    if actual == EmploymentType.NOT_SPECIFIED:
        if reject_not_specified:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason="Employment type could not be determined from the job description",
                details={"actual": actual},
            )
        return RuleVerdict(
            rule=RULE, passed=True,
            reason="Employment type is unspecified — accepted per config",
        )

    if allowed and actual not in allowed:
        return RuleVerdict(
            rule=RULE, passed=False,
            reason=f"Employment type '{actual}' is not in the allowed list {allowed}",
            details={"actual": actual, "allowed": allowed},
        )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Employment type '{actual}' is allowed",
        details={"actual": actual},
    )


def _check_work_mode(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "work_mode"

    allowed              = _require_list(cfg, "allowed", RULE)
    reject_not_specified = _require_bool(cfg, "reject_not_specified", RULE, default=False)

    if not allowed and not reject_not_specified:
        return RuleVerdict(rule=RULE, passed=True, reason="No constraints configured")

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    wm = parsed.work_mode
    actual = wm.value if hasattr(wm, "value") else str(wm)

    if actual == WorkMode.NOT_SPECIFIED:
        if reject_not_specified:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason="Work mode could not be determined from the job description",
                details={"actual": actual},
            )
        return RuleVerdict(
            rule=RULE, passed=True,
            reason="Work mode is unspecified — accepted per config",
        )

    if allowed and actual not in allowed:
        return RuleVerdict(
            rule=RULE, passed=False,
            reason=f"Work mode '{actual}' is not in the allowed list {allowed}",
            details={"actual": actual, "allowed": allowed},
        )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Work mode '{actual}' is allowed",
        details={"actual": actual},
    )


def _check_location(
    parsed: ParsedJobDescription | None,
    card_location: str | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "location"

    must_contain  = _require_list(cfg, "must_contain_any", RULE)
    case_sensitive = _require_bool(cfg, "case_sensitive", RULE, default=False)
    skip_if_remote = _require_bool(cfg, "skip_if_remote",  RULE, default=True)

    if not must_contain:
        return RuleVerdict(rule=RULE, passed=True, reason="No location constraints configured")

    # Bypass location check when the role is explicitly remote
    if skip_if_remote and parsed is not None:
        wm = parsed.work_mode
        wm_val = wm.value if hasattr(wm, "value") else str(wm)
        if wm_val == WorkMode.REMOTE:
            return RuleVerdict(
                rule=RULE, passed=True,
                reason="Location check skipped — role is remote",
            )

    # Resolve best available location string
    location = (parsed.location if parsed and parsed.location else None) or card_location

    if not location:
        return _missing_verdict(RULE, "location", policy)

    text = location if case_sensitive else location.lower()
    terms = must_contain if case_sensitive else [t.lower() for t in must_contain]

    matched = [t for t in terms if t in text]
    if matched:
        return RuleVerdict(
            rule=RULE, passed=True,
            reason=f"Location '{location}' matches {matched!r}",
            details={"location": location, "matched": matched},
        )

    return RuleVerdict(
        rule=RULE, passed=False,
        reason=(
            f"Location '{location}' does not contain any of "
            f"{must_contain!r}"
        ),
        details={"location": location, "required_any": must_contain},
    )


def _check_salary(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "salary"

    min_value        = cfg.get("min_value")
    period_str       = cfg.get("period", "annual")
    required_currency = (cfg.get("currency") or "").upper() or None
    reject_if_missing = _require_bool(cfg, "reject_if_missing", RULE, default=False)

    if min_value is None and required_currency is None:
        return RuleVerdict(rule=RULE, passed=True, reason="No salary constraints configured")

    if min_value is not None and not isinstance(min_value, (int, float)):
        raise RulesConfigError(f"Rule 'salary.min_value' must be a number, got {min_value!r}")

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    salary = parsed.salary

    if salary is None:
        if reject_if_missing:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason="No salary information found and reject_if_missing=true",
            )
        return RuleVerdict(
            rule=RULE, passed=True,
            reason="Salary information absent — accepted per config (reject_if_missing=false)",
        )

    # ── Currency check ────────────────────────────────────────────────────────
    if required_currency and salary.currency.upper() != required_currency:
        # Can't compare across currencies — apply missing-data policy
        return RuleVerdict(
            rule=RULE, passed=policy == "skip",
            reason=(
                f"Salary currency '{salary.currency}' differs from required "
                f"'{required_currency}' — cannot compare amounts"
            ),
            details={"actual_currency": salary.currency, "required_currency": required_currency},
        )

    # ── Amount check ──────────────────────────────────────────────────────────
    if min_value is not None:
        # Use max_value as proxy when min is absent (conservative: best case)
        job_amount = salary.min_value if salary.min_value is not None else salary.max_value

        if job_amount is None:
            # No numeric salary at all despite having a SalaryRange object
            if reject_if_missing:
                return RuleVerdict(
                    rule=RULE, passed=False,
                    reason="Salary range present but no numeric values found",
                )
            return RuleVerdict(
                rule=RULE, passed=True,
                reason="Salary has no numeric values — accepted per config",
            )

        # Normalise both amounts to annual for comparison
        job_annual    = _to_annual(job_amount, salary.period.value)
        config_annual = _to_annual(float(min_value), period_str)

        if job_annual < config_annual:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=(
                    f"Salary {job_amount} {salary.period.value} "
                    f"(= {job_annual:,.0f}/yr) is below the minimum "
                    f"{min_value} {period_str} (= {config_annual:,.0f}/yr)"
                ),
                details={
                    "job_amount": job_amount,
                    "job_period": salary.period.value,
                    "job_annual": job_annual,
                    "min_annual": config_annual,
                },
            )

        return RuleVerdict(
            rule=RULE, passed=True,
            reason=(
                f"Salary {job_amount} {salary.period.value} "
                f"(= {job_annual:,.0f}/yr) meets the minimum "
                f"{min_value} {period_str} (= {config_annual:,.0f}/yr)"
            ),
            details={
                "job_amount": job_amount,
                "job_period": salary.period.value,
                "job_annual": job_annual,
                "min_annual": config_annual,
            },
        )

    # Only a currency check was configured and it passed
    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Salary currency '{salary.currency}' matches required '{required_currency}'",
    )


def _check_skills(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "skills"

    must_have_any    = _require_list(cfg, "must_have_any",    RULE)
    must_have_all    = _require_list(cfg, "must_have_all",    RULE)
    must_not_have    = _require_list(cfg, "must_not_have_any", RULE)
    search_in        = cfg.get("search_in", "all")

    if search_in not in ("required", "preferred", "all"):
        raise RulesConfigError(
            f"Rule 'skills.search_in' must be 'required', 'preferred', or 'all' — "
            f"got {search_in!r}"
        )

    if not must_have_any and not must_have_all and not must_not_have:
        return RuleVerdict(rule=RULE, passed=True, reason="No skill constraints configured")

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    # Build the pool of skills to search in
    sk = parsed.skills
    if search_in == "required":
        pool: list[str] = list(sk.required)
    elif search_in == "preferred":
        pool = list(sk.preferred)
    else:  # "all"
        pool = sk.all_skills

    if not pool:
        return _missing_verdict(RULE, "skills", policy)

    # ── must_not_have_any (hard blocklist) ─────────────────────────────────────
    if must_not_have:
        blocked = [t for t in must_not_have if _any_skill_matches(t, pool)]
        if blocked:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=f"Job lists excluded skill(s): {blocked!r}",
                details={"blocked_found": blocked, "job_skills": pool},
            )

    # ── must_have_all ──────────────────────────────────────────────────────────
    if must_have_all:
        missing_all = [t for t in must_have_all if not _any_skill_matches(t, pool)]
        if missing_all:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=f"Job is missing required skill(s): {missing_all!r}",
                details={"missing": missing_all, "job_skills": pool},
            )

    # ── must_have_any ──────────────────────────────────────────────────────────
    if must_have_any:
        found = [t for t in must_have_any if _any_skill_matches(t, pool)]
        if not found:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=(
                    f"Job has none of the required technology terms {must_have_any!r}. "
                    f"Job skills: {pool!r}"
                ),
                details={"required_any": must_have_any, "job_skills": pool},
            )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason="All skill constraints satisfied",
        details={"job_skills": pool},
    )


def _check_experience(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "experience"

    max_required = cfg.get("max_years_required")
    min_required = cfg.get("min_years_required")

    if max_required is None and min_required is None:
        return RuleVerdict(rule=RULE, passed=True, reason="No experience constraints configured")

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    exp_min = parsed.experience_years_min  # minimum years the JD demands

    if exp_min is None:
        return _missing_verdict(RULE, "experience_years_min", policy)

    if max_required is not None and exp_min > max_required:
        return RuleVerdict(
            rule=RULE, passed=False,
            reason=(
                f"Job requires {exp_min}+ years of experience, which exceeds "
                f"the configured maximum of {max_required} years"
            ),
            details={"job_requires_min": exp_min, "config_max": max_required},
        )

    if min_required is not None and exp_min < min_required:
        return RuleVerdict(
            rule=RULE, passed=False,
            reason=(
                f"Job requires only {exp_min}+ years of experience, which is "
                f"below the configured minimum of {min_required} years"
            ),
            details={"job_requires_min": exp_min, "config_min": min_required},
        )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Experience requirement of {exp_min}+ years is within configured bounds",
        details={"job_requires_min": exp_min},
    )


def _check_title(
    job_title: str | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "title"

    must_contain     = _require_list(cfg, "must_contain_any",    RULE)
    must_not_contain = _require_list(cfg, "must_not_contain_any", RULE)
    case_sensitive   = _require_bool(cfg, "case_sensitive", RULE, default=False)

    if not must_contain and not must_not_contain:
        return RuleVerdict(rule=RULE, passed=True, reason="No title constraints configured")

    if not job_title:
        return _missing_verdict(RULE, "job_title", policy)

    text  = job_title if case_sensitive else job_title.lower()
    terms_case = lambda lst: lst if case_sensitive else [t.lower() for t in lst]  # noqa: E731

    # ── must_not_contain ──────────────────────────────────────────────────────
    if must_not_contain:
        blocked = [t for t in terms_case(must_not_contain) if t in text]
        if blocked:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=(
                    f"Job title '{job_title}' contains excluded term(s) "
                    f"{blocked!r}"
                ),
                details={"title": job_title, "blocked_found": blocked},
            )

    # ── must_contain_any ─────────────────────────────────────────────────────
    if must_contain:
        matched = [t for t in terms_case(must_contain) if t in text]
        if not matched:
            return RuleVerdict(
                rule=RULE, passed=False,
                reason=(
                    f"Job title '{job_title}' does not contain any of "
                    f"{must_contain!r}"
                ),
                details={"title": job_title, "required_any": must_contain},
            )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Title '{job_title}' passes all title constraints",
        details={"title": job_title},
    )


def _check_confidence(
    parsed: ParsedJobDescription | None,
    cfg: dict,
    policy: Literal["skip", "fail"],
) -> RuleVerdict:
    RULE = "confidence"

    min_score = cfg.get("min_score")
    if min_score is None:
        return RuleVerdict(rule=RULE, passed=True, reason="No confidence threshold configured")

    if not isinstance(min_score, (int, float)) or not (0.0 <= min_score <= 1.0):
        raise RulesConfigError(
            f"Rule 'confidence.min_score' must be a float in [0.0, 1.0], got {min_score!r}"
        )

    if parsed is None:
        return _missing_verdict(RULE, "parsed", policy)

    score = parsed.confidence_score
    if score < min_score:
        return RuleVerdict(
            rule=RULE, passed=False,
            reason=(
                f"Gemini confidence score {score:.2f} is below the "
                f"required minimum of {min_score:.2f}"
            ),
            details={"score": score, "min_score": min_score},
        )

    return RuleVerdict(
        rule=RULE, passed=True,
        reason=f"Gemini confidence score {score:.2f} meets the threshold {min_score:.2f}",
        details={"score": score},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher map  (rule-name → checker function)
# ══════════════════════════════════════════════════════════════════════════════

# Signature: (parsed, card_title, card_location, rule_cfg, policy) → RuleVerdict
# We normalise all inputs here so checkers only see what they need.

_RULE_ORDER: list[str] = [
    "confidence",
    "employment_type",
    "work_mode",
    "location",
    "salary",
    "skills",
    "experience",
    "title",
]


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def apply_qualification_rules(
    job: "EnrichedLinkedInJob | ParsedJobDescription",
    config: dict[str, Any],
    on_missing_data: Literal["skip", "fail"] = "skip",
) -> QualificationResult:
    """
    Apply every rule group defined in *config* to *job*.

    Parameters
    ----------
    job
        Either an :class:`~app.models.linkedin.EnrichedLinkedInJob`
        (card data + optional Gemini parse) or a raw
        :class:`~app.models.job_parser.ParsedJobDescription`.
        Both are accepted so the function can be used in the harvest
        pipeline as well as in post-hoc filtering of stored jobs.
    config
        Rules config dict.  See module docstring for the full schema.
        Unknown top-level keys are silently ignored.
    on_missing_data
        Controls what happens when a rule needs a field that is absent
        in the job data:

        ``"skip"``  — treat the verdict as *passed* (permissive, default).
        ``"fail"``  — treat the verdict as *failed* (strict mode).

    Returns
    -------
    QualificationResult
        ``.passed`` is True iff every rule verdict passed.
    """
    if not isinstance(config, dict):
        raise RulesConfigError(f"Rules config must be a dict, got {type(config).__name__}")

    # ── Normalise input ───────────────────────────────────────────────────────
    if isinstance(job, ParsedJobDescription):
        parsed:         ParsedJobDescription | None = job
        card_location:  str | None                 = parsed.location
        job_id:         str | None                 = None
        job_title_raw:  str | None                 = parsed.job_title
    else:
        # EnrichedLinkedInJob
        parsed        = job.parsed
        card_location = job.location
        job_id        = job.job_id
        job_title_raw = (parsed.job_title if parsed and parsed.job_title else None) or job.job_title

    # ── Evaluate each configured rule group in a deterministic order ──────────
    verdicts: list[RuleVerdict] = []

    for rule_name in _RULE_ORDER:
        rule_cfg = config.get(rule_name)
        if rule_cfg is None:
            continue  # rule group not configured — skip

        if not isinstance(rule_cfg, dict):
            raise RulesConfigError(
                f"Rule '{rule_name}' must be a dict of options, "
                f"got {type(rule_cfg).__name__}"
            )

        if rule_name == "confidence":
            verdict = _check_confidence(parsed, rule_cfg, on_missing_data)
        elif rule_name == "employment_type":
            verdict = _check_employment_type(parsed, rule_cfg, on_missing_data)
        elif rule_name == "work_mode":
            verdict = _check_work_mode(parsed, rule_cfg, on_missing_data)
        elif rule_name == "location":
            verdict = _check_location(parsed, card_location, rule_cfg, on_missing_data)
        elif rule_name == "salary":
            verdict = _check_salary(parsed, rule_cfg, on_missing_data)
        elif rule_name == "skills":
            verdict = _check_skills(parsed, rule_cfg, on_missing_data)
        elif rule_name == "experience":
            verdict = _check_experience(parsed, rule_cfg, on_missing_data)
        elif rule_name == "title":
            verdict = _check_title(job_title_raw, rule_cfg, on_missing_data)
        else:
            continue  # defensive — unreachable given _RULE_ORDER

        verdicts.append(verdict)

    passed = all(v.passed for v in verdicts)

    return QualificationResult(
        passed    = passed,
        job_id    = job_id,
        job_title = job_title_raw,
        verdicts  = verdicts,
    )


def load_rules(path: str | Path) -> dict[str, Any]:
    """
    Load a qualification rules config from a JSON file.

    Parameters
    ----------
    path
        Absolute or relative path to the JSON file.

    Returns
    -------
    dict
        Parsed config ready to pass to :func:`apply_qualification_rules`.

    Raises
    ------
    RulesConfigError
        If the file cannot be read or is not valid JSON.
    FileNotFoundError
        If the path does not exist.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"Rules file not found: {p}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulesConfigError(f"Rules file is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RulesConfigError(
            f"Rules file must contain a JSON object at the top level, "
            f"got {type(data).__name__}"
        )

    return data


def filter_jobs(
    jobs: list["EnrichedLinkedInJob | ParsedJobDescription"],
    config: dict[str, Any],
    on_missing_data: Literal["skip", "fail"] = "skip",
) -> tuple[
    list["EnrichedLinkedInJob | ParsedJobDescription"],
    list[QualificationResult],
]:
    """
    Bulk-filter *jobs* against *config*, returning only those that pass.

    Parameters
    ----------
    jobs
        Any mix of EnrichedLinkedInJob and ParsedJobDescription instances.
    config
        Rules config dict.
    on_missing_data
        Passed through to :func:`apply_qualification_rules`.

    Returns
    -------
    tuple[list[job], list[QualificationResult]]
        ``(qualified_jobs, all_results)`` — *qualified_jobs* contains only
        the jobs whose result ``.passed`` is True; *all_results* contains
        one result per input job (same order as *jobs*).
    """
    results: list[QualificationResult] = [
        apply_qualification_rules(job, config, on_missing_data) for job in jobs
    ]
    qualified = [job for job, res in zip(jobs, results) if res.passed]
    return qualified, results
