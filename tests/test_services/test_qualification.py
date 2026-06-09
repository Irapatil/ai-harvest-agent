"""
Tests for app.services.qualification — hard qualification rules engine.

Coverage
────────
  _skill_matches           word-boundary behaviour, multi-word terms
  _to_annual               period normalisation
  _check_employment_type   allowed list, not_specified handling
  _check_work_mode         allowed list, not_specified handling
  _check_location          substring match, remote bypass, case
  _check_salary            GBP floor, currency mismatch, missing salary
  _check_skills            must_have_any/all, must_not_have, search_in
  _check_experience        max / min year bounds
  _check_title             must_contain / must_not_contain
  _check_confidence        threshold
  apply_qualification_rules accepts EnrichedLinkedInJob or ParsedJobDescription
  filter_jobs              bulk filter
  load_rules               JSON loading + error cases
  QualificationResult      score, violations, as_dict, repr
  on_missing_data          skip vs fail
  config errors            RulesConfigError on bad types
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.models.job_parser import (
    EmploymentType,
    ParsedJobDescription,
    SalaryPeriod,
    SalaryRange,
    SkillSet,
    WorkMode,
)
from app.models.linkedin import EnrichedLinkedInJob
from app.services.qualification import (
    QualificationResult,
    RuleVerdict,
    RulesConfigError,
    _skill_matches,
    _to_annual,
    apply_qualification_rules,
    filter_jobs,
    load_rules,
)


# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ══════════════════════════════════════════════════════════════════════════════

def _parsed(
    *,
    job_title: str | None = "Senior Java Developer",
    employment_type: EmploymentType = EmploymentType.CONTRACT,
    work_mode: WorkMode = WorkMode.HYBRID,
    location: str | None = "London, UK",
    salary: SalaryRange | None = SalaryRange(
        min_value=500, max_value=650, currency="GBP", period=SalaryPeriod.DAILY
    ),
    skills: SkillSet | None = None,
    experience_years_min: int | None = 5,
    experience_years_max: int | None = None,
    confidence_score: float = 0.90,
) -> ParsedJobDescription:
    return ParsedJobDescription(
        job_title            = job_title,
        employment_type      = employment_type,
        work_mode            = work_mode,
        location             = location,
        salary               = salary,
        skills               = skills or SkillSet(
            required  = ["Java 17", "Spring Boot", "Kafka", "Docker"],
            preferred = ["AWS", "Terraform", "Kubernetes"],
        ),
        experience_years_min = experience_years_min,
        experience_years_max = experience_years_max,
        confidence_score     = confidence_score,
    )


_MISSING = object()  # sentinel so _enriched(parsed=None) ≠ _enriched()


def _enriched(parsed=_MISSING, **kw) -> EnrichedLinkedInJob:
    p: ParsedJobDescription | None = _parsed() if parsed is _MISSING else parsed
    return EnrichedLinkedInJob(
        job_id      = kw.get("job_id", "1234567890"),
        job_title   = kw.get("job_title", "Senior Java Developer"),
        company     = kw.get("company", "Acme Corp"),
        location    = kw.get("location", "London, UK"),
        job_url     = "https://www.linkedin.com/jobs/view/1234567890",
        posted_time = "2026-05-27T09:00:00",
        raw_description     = "Full job description text here...",
        description_length  = 35,
        parsed              = p,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestSkillMatches:
    def test_exact_match(self):
        assert _skill_matches("Java", "Java") is True

    def test_substring_version_number(self):
        assert _skill_matches("Java", "Java 17") is True

    def test_prefix_word(self):
        assert _skill_matches("Java", "Core Java") is True

    def test_no_match_on_javascript(self):
        """'Java' must NOT match 'JavaScript' — word boundary prevents it."""
        assert _skill_matches("Java", "JavaScript") is False

    def test_case_insensitive(self):
        assert _skill_matches("python", "Python 3.12") is True
        assert _skill_matches("PYTHON", "python") is True

    def test_multi_word_term(self):
        assert _skill_matches("Spring Boot", "Spring Boot 3") is True
        assert _skill_matches("Spring Boot", "Spring Boot") is True

    def test_multi_word_partial_no_match(self):
        assert _skill_matches("Spring Boot", "Spring MVC") is False

    def test_no_match_different_skill(self):
        assert _skill_matches("Go", "PostgreSQL") is False


class TestToAnnual:
    def test_annual_unchanged(self):
        assert _to_annual(80_000, "annual") == 80_000.0

    def test_daily_to_annual(self):
        assert _to_annual(500, "daily") == pytest.approx(130_000.0)

    def test_monthly_to_annual(self):
        assert _to_annual(5_000, "monthly") == pytest.approx(60_000.0)

    def test_hourly_to_annual(self):
        assert _to_annual(50, "hourly") == pytest.approx(104_000.0)

    def test_weekly_to_annual(self):
        assert _to_annual(2_000, "weekly") == pytest.approx(104_000.0)

    def test_invalid_period_raises(self):
        with pytest.raises(RulesConfigError, match="Unknown salary period"):
            _to_annual(100, "fortnightly")


# ══════════════════════════════════════════════════════════════════════════════
# employment_type rule
# ══════════════════════════════════════════════════════════════════════════════

class TestEmploymentType:
    _cfg = {"employment_type": {"allowed": ["contract", "freelance"]}}

    def test_contract_passes(self):
        result = apply_qualification_rules(_parsed(employment_type=EmploymentType.CONTRACT), self._cfg)
        assert result.passed is True

    def test_permanent_fails(self):
        result = apply_qualification_rules(_parsed(employment_type=EmploymentType.PERMANENT), self._cfg)
        assert result.passed is False
        assert any("permanent" in v.reason.lower() or "not in the allowed" in v.reason for v in result.violations)

    def test_not_specified_fails_by_default(self):
        result = apply_qualification_rules(
            _parsed(employment_type=EmploymentType.NOT_SPECIFIED),
            self._cfg,
        )
        assert result.passed is False

    def test_not_specified_passes_when_configured(self):
        cfg = {"employment_type": {"allowed": ["contract"], "reject_not_specified": False}}
        result = apply_qualification_rules(
            _parsed(employment_type=EmploymentType.NOT_SPECIFIED), cfg
        )
        assert result.passed is True

    def test_no_parsed_skip(self):
        """on_missing_data='skip' → pass when parsed is None."""
        result = apply_qualification_rules(
            ParsedJobDescription(employment_type=EmploymentType.NOT_SPECIFIED),
            {"employment_type": {"allowed": ["contract"]}},
        )
        # NOT_SPECIFIED + reject_not_specified=True (default) → fail
        assert result.passed is False

    def test_enriched_job_accepted(self):
        result = apply_qualification_rules(_enriched(), self._cfg)
        assert result.passed is True

    def test_freelance_in_allowed_passes(self):
        result = apply_qualification_rules(
            _parsed(employment_type=EmploymentType.FREELANCE), self._cfg
        )
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# work_mode rule
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkMode:
    _cfg = {"work_mode": {"allowed": ["remote", "hybrid"]}}

    def test_hybrid_passes(self):
        result = apply_qualification_rules(_parsed(work_mode=WorkMode.HYBRID), self._cfg)
        assert result.passed is True

    def test_onsite_fails(self):
        result = apply_qualification_rules(_parsed(work_mode=WorkMode.ONSITE), self._cfg)
        assert result.passed is False

    def test_not_specified_skipped_by_default(self):
        """reject_not_specified defaults to False for work_mode."""
        result = apply_qualification_rules(
            _parsed(work_mode=WorkMode.NOT_SPECIFIED), self._cfg
        )
        assert result.passed is True

    def test_not_specified_fails_when_configured(self):
        cfg = {"work_mode": {"allowed": ["remote"], "reject_not_specified": True}}
        result = apply_qualification_rules(_parsed(work_mode=WorkMode.NOT_SPECIFIED), cfg)
        assert result.passed is False


# ══════════════════════════════════════════════════════════════════════════════
# location rule
# ══════════════════════════════════════════════════════════════════════════════

class TestLocation:
    _cfg = {"location": {"must_contain_any": ["London", "Manchester", "UK", "Remote"]}}

    def test_london_passes(self):
        result = apply_qualification_rules(_parsed(location="London, UK"), self._cfg)
        assert result.passed is True

    def test_edinburgh_fails(self):
        result = apply_qualification_rules(_parsed(location="Edinburgh, Scotland"), self._cfg)
        assert result.passed is False

    def test_case_insensitive_default(self):
        result = apply_qualification_rules(_parsed(location="london, uk"), self._cfg)
        assert result.passed is True

    def test_case_sensitive_exact(self):
        cfg = {"location": {"must_contain_any": ["London"], "case_sensitive": True}}
        assert apply_qualification_rules(_parsed(location="london, uk"), cfg).passed is False
        assert apply_qualification_rules(_parsed(location="London, UK"), cfg).passed is True

    def test_remote_work_mode_bypasses_location(self):
        """When work_mode=remote and skip_if_remote=true, location is irrelevant."""
        result = apply_qualification_rules(
            _parsed(work_mode=WorkMode.REMOTE, location="Edinburgh"),
            self._cfg,
        )
        assert result.passed is True

    def test_skip_if_remote_disabled(self):
        """Explicitly disable bypass → location must still match."""
        cfg = {
            "location": {
                "must_contain_any": ["London"],
                "skip_if_remote": False,
            }
        }
        result = apply_qualification_rules(
            _parsed(work_mode=WorkMode.REMOTE, location="Edinburgh"), cfg
        )
        assert result.passed is False

    def test_no_location_skip_policy(self):
        result = apply_qualification_rules(
            _parsed(location=None),
            self._cfg,
            on_missing_data="skip",
        )
        assert result.passed is True

    def test_no_location_fail_policy(self):
        result = apply_qualification_rules(
            _parsed(location=None),
            self._cfg,
            on_missing_data="fail",
        )
        assert result.passed is False

    def test_card_location_used_as_fallback(self):
        """When parsed.location is None, EnrichedLinkedInJob.location is used."""
        parsed_no_loc = _parsed(location=None)
        job = _enriched(parsed=parsed_no_loc, location="London, UK")
        result = apply_qualification_rules(job, self._cfg)
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# salary rule
# ══════════════════════════════════════════════════════════════════════════════

class TestSalary:
    _cfg = {"salary": {"min_value": 400, "period": "daily", "currency": "GBP"}}

    def test_above_floor_passes(self):
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=500, currency="GBP", period=SalaryPeriod.DAILY)),
            self._cfg,
        )
        assert result.passed is True

    def test_below_floor_fails(self):
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=300, currency="GBP", period=SalaryPeriod.DAILY)),
            self._cfg,
        )
        assert result.passed is False
        assert any("below" in v.reason.lower() for v in result.violations)

    def test_exact_floor_passes(self):
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=400, currency="GBP", period=SalaryPeriod.DAILY)),
            self._cfg,
        )
        assert result.passed is True

    def test_annual_normalised_correctly(self):
        """A £100k/yr salary converts to ~£384/day which is < £400 floor."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=100_000, currency="GBP", period=SalaryPeriod.ANNUAL)),
            self._cfg,
        )
        # 100_000 / 260 ≈ 384.6 which is below 400 daily
        assert result.passed is False

    def test_annual_above_floor(self):
        """£110k/yr ≈ £423/day — just above £400 daily floor."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=110_000, currency="GBP", period=SalaryPeriod.ANNUAL)),
            self._cfg,
        )
        # 110_000 / 260 ≈ 423.1 > 400
        assert result.passed is True

    def test_currency_mismatch_skip(self):
        """USD salary vs GBP requirement → skip (can't compare)."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=700, currency="USD", period=SalaryPeriod.DAILY)),
            self._cfg,
            on_missing_data="skip",
        )
        assert result.passed is True

    def test_currency_mismatch_fail(self):
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=700, currency="USD", period=SalaryPeriod.DAILY)),
            self._cfg,
            on_missing_data="fail",
        )
        assert result.passed is False

    def test_missing_salary_accepted_by_default(self):
        result = apply_qualification_rules(_parsed(salary=None), self._cfg)
        assert result.passed is True

    def test_missing_salary_rejected_when_configured(self):
        cfg = {"salary": {"min_value": 400, "period": "daily", "reject_if_missing": True}}
        result = apply_qualification_rules(_parsed(salary=None), cfg)
        assert result.passed is False

    def test_uses_min_value_for_comparison(self):
        """When salary.min_value is set, compare against it (not max_value)."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=450, max_value=700, currency="GBP", period=SalaryPeriod.DAILY)),
            self._cfg,
        )
        assert result.passed is True

    def test_falls_back_to_max_when_min_is_none(self):
        """When salary.min_value is None, max_value is used as conservative proxy."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=None, max_value=450, currency="GBP", period=SalaryPeriod.DAILY)),
            self._cfg,
        )
        assert result.passed is True

    def test_currency_only_check(self):
        cfg = {"salary": {"currency": "GBP"}}
        assert apply_qualification_rules(
            _parsed(salary=SalaryRange(currency="GBP", period=SalaryPeriod.DAILY)), cfg
        ).passed is True

    def test_hourly_converted_correctly(self):
        """£60/hr * 2080 = £124,800/yr ÷ 260 ≈ £480/day > £400."""
        result = apply_qualification_rules(
            _parsed(salary=SalaryRange(min_value=60, currency="GBP", period=SalaryPeriod.HOURLY)),
            self._cfg,
        )
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# skills rule
# ══════════════════════════════════════════════════════════════════════════════

class TestSkills:
    _java_cfg = {"skills": {"must_have_any": ["Java", "Kotlin"]}}

    def test_must_have_any_passes(self):
        result = apply_qualification_rules(_parsed(), self._java_cfg)
        assert result.passed is True

    def test_must_have_any_fails_none_present(self):
        parsed = _parsed(skills=SkillSet(required=["Python", "Django"]))
        result = apply_qualification_rules(parsed, self._java_cfg)
        assert result.passed is False
        assert any("none of" in v.reason.lower() for v in result.violations)

    def test_java_does_not_match_javascript(self):
        """Word-boundary rule: 'Java' must NOT match 'JavaScript'."""
        parsed = _parsed(skills=SkillSet(required=["JavaScript", "TypeScript"]))
        result = apply_qualification_rules(parsed, self._java_cfg)
        assert result.passed is False

    def test_must_have_all_passes(self):
        cfg = {"skills": {"must_have_all": ["Java", "Docker"]}}
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is True

    def test_must_have_all_fails_one_missing(self):
        cfg = {"skills": {"must_have_all": ["Java", "Rust"]}}
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is False
        assert any("Rust" in v.reason for v in result.violations)

    def test_must_not_have_fails_on_match(self):
        cfg = {"skills": {"must_not_have_any": ["COBOL"]}}
        parsed = _parsed(skills=SkillSet(required=["COBOL", "JCL"]))
        result = apply_qualification_rules(parsed, cfg)
        assert result.passed is False
        assert any("COBOL" in v.reason for v in result.violations)

    def test_must_not_have_passes_when_absent(self):
        cfg = {"skills": {"must_not_have_any": ["COBOL"]}}
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is True

    def test_search_in_required_only(self):
        """When search_in='required', preferred skills are ignored."""
        cfg = {"skills": {"must_have_any": ["AWS"], "search_in": "required"}}
        # AWS is in preferred, not required
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is False

    def test_search_in_preferred_only(self):
        cfg = {"skills": {"must_have_any": ["AWS"], "search_in": "preferred"}}
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is True

    def test_search_in_all_default(self):
        """Default searches required + preferred."""
        cfg = {"skills": {"must_have_any": ["Terraform"]}}
        result = apply_qualification_rules(_parsed(), cfg)   # Terraform is in preferred
        assert result.passed is True

    def test_empty_skills_missing_data(self):
        parsed = _parsed(skills=SkillSet(required=[], preferred=[]))
        result = apply_qualification_rules(parsed, self._java_cfg, on_missing_data="fail")
        assert result.passed is False

    def test_invalid_search_in_raises(self):
        cfg = {"skills": {"search_in": "mandatory"}}
        with pytest.raises(RulesConfigError, match="search_in"):
            apply_qualification_rules(_parsed(), cfg)

    def test_combined_rules_all_must_pass(self):
        """must_have_all + must_not_have_any both evaluated."""
        cfg = {
            "skills": {
                "must_have_all":    ["Java", "Docker"],
                "must_not_have_any": ["COBOL"],
            }
        }
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# experience rule
# ══════════════════════════════════════════════════════════════════════════════

class TestExperience:
    def test_within_max_passes(self):
        cfg = {"experience": {"max_years_required": 10}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is True

    def test_exceeds_max_fails(self):
        cfg = {"experience": {"max_years_required": 3}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is False
        assert any("exceeds" in v.reason for v in result.violations)

    def test_exactly_at_max_passes(self):
        cfg = {"experience": {"max_years_required": 5}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is True

    def test_below_min_fails(self):
        cfg = {"experience": {"min_years_required": 7}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is False
        assert any("below" in v.reason for v in result.violations)

    def test_above_min_passes(self):
        cfg = {"experience": {"min_years_required": 3}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is True

    def test_both_bounds_pass(self):
        cfg = {"experience": {"min_years_required": 3, "max_years_required": 10}}
        result = apply_qualification_rules(_parsed(experience_years_min=5), cfg)
        assert result.passed is True

    def test_no_experience_field_skip(self):
        cfg = {"experience": {"max_years_required": 10}}
        result = apply_qualification_rules(
            _parsed(experience_years_min=None), cfg, on_missing_data="skip"
        )
        assert result.passed is True

    def test_no_experience_field_fail(self):
        cfg = {"experience": {"max_years_required": 10}}
        result = apply_qualification_rules(
            _parsed(experience_years_min=None), cfg, on_missing_data="fail"
        )
        assert result.passed is False


# ══════════════════════════════════════════════════════════════════════════════
# title rule
# ══════════════════════════════════════════════════════════════════════════════

class TestTitle:
    def test_must_contain_passes(self):
        cfg = {"title": {"must_contain_any": ["Developer", "Engineer"]}}
        result = apply_qualification_rules(_parsed(job_title="Senior Java Developer"), cfg)
        assert result.passed is True

    def test_must_contain_fails(self):
        cfg = {"title": {"must_contain_any": ["Manager", "Director"]}}
        result = apply_qualification_rules(_parsed(job_title="Senior Java Developer"), cfg)
        assert result.passed is False

    def test_must_not_contain_passes(self):
        cfg = {"title": {"must_not_contain_any": ["Junior", "Graduate"]}}
        result = apply_qualification_rules(_parsed(job_title="Senior Java Developer"), cfg)
        assert result.passed is True

    def test_must_not_contain_fails(self):
        cfg = {"title": {"must_not_contain_any": ["Junior", "Graduate"]}}
        result = apply_qualification_rules(_parsed(job_title="Junior Java Developer"), cfg)
        assert result.passed is False
        assert any("junior" in v.reason.lower() for v in result.violations)

    def test_case_insensitive_default(self):
        cfg = {"title": {"must_contain_any": ["developer"]}}
        result = apply_qualification_rules(_parsed(job_title="Senior Java Developer"), cfg)
        assert result.passed is True

    def test_case_sensitive_no_match(self):
        cfg = {"title": {"must_contain_any": ["developer"], "case_sensitive": True}}
        result = apply_qualification_rules(_parsed(job_title="Senior Java Developer"), cfg)
        assert result.passed is False

    def test_no_title_skip(self):
        cfg = {"title": {"must_contain_any": ["Developer"]}}
        result = apply_qualification_rules(_parsed(job_title=None), cfg, on_missing_data="skip")
        assert result.passed is True

    def test_uses_parsed_title_from_enriched(self):
        """EnrichedLinkedInJob: parsed.job_title takes priority over card title."""
        p = _parsed(job_title="Senior Java Developer")
        job = _enriched(parsed=p, job_title="Contract Java Dev")  # card title differs
        cfg = {"title": {"must_not_contain_any": ["Junior"]}}
        result = apply_qualification_rules(job, cfg)
        assert result.passed is True

    def test_falls_back_to_card_title(self):
        """If parsed.job_title is None, card title is used."""
        p = _parsed(job_title=None)
        job = _enriched(parsed=p, job_title="Senior Java Developer")
        cfg = {"title": {"must_contain_any": ["Developer"]}}
        result = apply_qualification_rules(job, cfg)
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# confidence rule
# ══════════════════════════════════════════════════════════════════════════════

class TestConfidence:
    def test_above_threshold_passes(self):
        cfg = {"confidence": {"min_score": 0.7}}
        result = apply_qualification_rules(_parsed(confidence_score=0.90), cfg)
        assert result.passed is True

    def test_below_threshold_fails(self):
        cfg = {"confidence": {"min_score": 0.7}}
        result = apply_qualification_rules(_parsed(confidence_score=0.50), cfg)
        assert result.passed is False

    def test_exact_threshold_passes(self):
        cfg = {"confidence": {"min_score": 0.9}}
        result = apply_qualification_rules(_parsed(confidence_score=0.9), cfg)
        assert result.passed is True

    def test_invalid_threshold_raises(self):
        cfg = {"confidence": {"min_score": 1.5}}
        with pytest.raises(RulesConfigError, match="min_score"):
            apply_qualification_rules(_parsed(), cfg)

    def test_no_parsed_skip(self):
        cfg = {"confidence": {"min_score": 0.7}}
        job = _enriched(parsed=None)
        result = apply_qualification_rules(job, cfg, on_missing_data="skip")
        assert result.passed is True

    def test_no_parsed_fail(self):
        cfg = {"confidence": {"min_score": 0.7}}
        job = _enriched(parsed=None)
        result = apply_qualification_rules(job, cfg, on_missing_data="fail")
        assert result.passed is False


# ══════════════════════════════════════════════════════════════════════════════
# apply_qualification_rules — integration
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyQualificationRules:

    FULL_CONFIG = {
        "employment_type": {"allowed": ["contract", "freelance"]},
        "work_mode":       {"allowed": ["remote", "hybrid"]},
        "location":        {"must_contain_any": ["London", "UK", "Remote"]},
        "salary":          {"min_value": 400, "period": "daily", "currency": "GBP"},
        "skills":          {"must_have_any": ["Java", "Python"], "must_not_have_any": ["COBOL"]},
        "experience":      {"max_years_required": 12},
        "title":           {"must_not_contain_any": ["Junior", "Graduate"]},
        "confidence":      {"min_score": 0.6},
    }

    def test_ideal_job_passes_all_rules(self):
        result = apply_qualification_rules(_parsed(), self.FULL_CONFIG)
        assert result.passed is True
        assert len(result.violations) == 0

    def test_violations_accumulate(self):
        """Multiple failing rules all appear in violations list."""
        bad = _parsed(
            employment_type  = EmploymentType.PERMANENT,
            work_mode        = WorkMode.ONSITE,
            location         = "Edinburgh, Scotland",
            confidence_score = 0.2,
        )
        result = apply_qualification_rules(bad, self.FULL_CONFIG)
        assert result.passed is False
        assert len(result.violations) >= 3

    def test_empty_config_always_passes(self):
        result = apply_qualification_rules(_parsed(), {})
        assert result.passed is True
        assert result.score == 1.0

    def test_unknown_keys_ignored(self):
        cfg = {"unknown_rule": {"foo": "bar"}}
        result = apply_qualification_rules(_parsed(), cfg)
        assert result.passed is True

    def test_accepts_bare_parsed_job_description(self):
        result = apply_qualification_rules(
            _parsed(), {"employment_type": {"allowed": ["contract"]}}
        )
        assert result.passed is True

    def test_accepts_enriched_linkedin_job(self):
        result = apply_qualification_rules(
            _enriched(), {"employment_type": {"allowed": ["contract"]}}
        )
        assert result.passed is True

    def test_invalid_config_type_raises(self):
        with pytest.raises(RulesConfigError, match="must be a dict"):
            apply_qualification_rules(_parsed(), "invalid")  # type: ignore

    def test_invalid_rule_value_type_raises(self):
        with pytest.raises(RulesConfigError):
            apply_qualification_rules(_parsed(), {"employment_type": "contract"})

    def test_result_verdicts_ordered_by_rule_order(self):
        result = apply_qualification_rules(_parsed(), self.FULL_CONFIG)
        names = [v.rule for v in result.verdicts]
        # confidence should come before employment_type (per _RULE_ORDER)
        assert names.index("confidence") < names.index("employment_type")


# ══════════════════════════════════════════════════════════════════════════════
# QualificationResult  methods
# ══════════════════════════════════════════════════════════════════════════════

class TestQualificationResult:

    def _make(self, verdicts: list[tuple[str, bool, str]]) -> QualificationResult:
        vs = [RuleVerdict(rule=r, passed=p, reason=m) for r, p, m in verdicts]
        return QualificationResult(
            passed=all(v.passed for v in vs),
            job_id="jid",
            job_title="Test Job",
            verdicts=vs,
        )

    def test_score_all_pass(self):
        r = self._make([("a", True, "ok"), ("b", True, "ok")])
        assert r.score == 1.0

    def test_score_half_pass(self):
        r = self._make([("a", True, "ok"), ("b", False, "bad")])
        assert r.score == pytest.approx(0.5)

    def test_score_empty_verdicts(self):
        r = QualificationResult(passed=True, job_id=None, job_title=None, verdicts=[])
        assert r.score == 1.0

    def test_violations_subset(self):
        r = self._make([("a", True, "ok"), ("b", False, "bad"), ("c", False, "bad")])
        assert len(r.violations) == 2
        assert all(not v.passed for v in r.violations)

    def test_passes_subset(self):
        r = self._make([("a", True, "ok"), ("b", False, "bad")])
        assert len(r.passes) == 1
        assert r.passes[0].rule == "a"

    def test_as_dict_shape(self):
        r = self._make([("salary", True, "ok"), ("skills", False, "missing java")])
        d = r.as_dict()
        assert d["passed"] is False
        assert d["job_id"] == "jid"
        assert "score" in d
        assert len(d["violations"]) == 1
        assert d["violations"][0]["rule"] == "skills"
        assert len(d["verdicts"]) == 2

    def test_repr_pass(self):
        r = self._make([("a", True, "ok")])
        assert "PASS" in repr(r)

    def test_repr_fail(self):
        r = self._make([("a", False, "bad")])
        assert "FAIL" in repr(r)

    def test_rule_verdict_str(self):
        v = RuleVerdict(rule="salary", passed=False, reason="too low")
        assert "✗" in str(v)
        assert "salary" in str(v)
        assert "too low" in str(v)


# ══════════════════════════════════════════════════════════════════════════════
# filter_jobs
# ══════════════════════════════════════════════════════════════════════════════

class TestFilterJobs:

    _cfg = {"employment_type": {"allowed": ["contract"]}}

    def test_filters_out_permanent(self):
        jobs = [
            _parsed(employment_type=EmploymentType.CONTRACT),
            _parsed(employment_type=EmploymentType.PERMANENT),
            _parsed(employment_type=EmploymentType.CONTRACT),
        ]
        qualified, results = filter_jobs(jobs, self._cfg)
        assert len(qualified) == 2
        assert len(results) == 3

    def test_results_in_same_order(self):
        jobs = [
            _parsed(job_title="A", employment_type=EmploymentType.PERMANENT),
            _parsed(job_title="B", employment_type=EmploymentType.CONTRACT),
        ]
        qualified, results = filter_jobs(jobs, self._cfg)
        assert results[0].passed is False
        assert results[1].passed is True
        assert len(qualified) == 1

    def test_empty_input(self):
        qualified, results = filter_jobs([], self._cfg)
        assert qualified == []
        assert results == []

    def test_all_pass(self):
        jobs = [_parsed(employment_type=EmploymentType.CONTRACT)] * 3
        qualified, results = filter_jobs(jobs, self._cfg)
        assert len(qualified) == 3
        assert all(r.passed for r in results)

    def test_accepts_enriched_jobs(self):
        jobs = [_enriched(), _enriched()]
        qualified, results = filter_jobs(jobs, self._cfg)
        assert len(qualified) == 2


# ══════════════════════════════════════════════════════════════════════════════
# load_rules
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadRules:

    def test_loads_valid_json(self, tmp_path: Path):
        rules = {"employment_type": {"allowed": ["contract"]}}
        p = tmp_path / "rules.json"
        p.write_text(json.dumps(rules), encoding="utf-8")
        loaded = load_rules(p)
        assert loaded == rules

    def test_string_path_accepted(self, tmp_path: Path):
        p = tmp_path / "rules.json"
        p.write_text('{"confidence": {"min_score": 0.5}}', encoding="utf-8")
        loaded = load_rules(str(p))
        assert loaded["confidence"]["min_score"] == 0.5

    def test_file_not_found_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_rules(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{bad json}", encoding="utf-8")
        with pytest.raises(RulesConfigError, match="not valid JSON"):
            load_rules(p)

    def test_non_object_json_raises(self, tmp_path: Path):
        p = tmp_path / "array.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(RulesConfigError, match="JSON object"):
            load_rules(p)


# ══════════════════════════════════════════════════════════════════════════════
# on_missing_data policy — comprehensive
# ══════════════════════════════════════════════════════════════════════════════

class TestMissingDataPolicy:
    """Verify the skip/fail policy propagates consistently across all rules."""

    _no_parse_job = EnrichedLinkedInJob(
        job_id="x", job_title="Unknown Job", company="Unknown",
        location="London", job_url="https://example.com",
        parsed=None,
    )

    _full_config = {
        "employment_type": {"allowed": ["contract"]},
        "work_mode":       {"allowed": ["remote"]},
        "salary":          {"min_value": 400, "period": "daily"},
        "skills":          {"must_have_any": ["Java"]},
        "experience":      {"max_years_required": 10},
        "confidence":      {"min_score": 0.5},
    }

    def test_all_rules_skip_when_no_parse(self):
        result = apply_qualification_rules(
            self._no_parse_job, self._full_config, on_missing_data="skip"
        )
        assert result.passed is True
        # Every verdict should be "skipped" (passed=True)
        assert all(v.passed for v in result.verdicts)

    def test_all_rules_fail_when_no_parse(self):
        result = apply_qualification_rules(
            self._no_parse_job, self._full_config, on_missing_data="fail"
        )
        # Every rule that needs parsed data should fail
        assert result.passed is False
        assert len(result.violations) >= 4
