"""
All Pydantic models for the job-parser service.

Hierarchy
─────────
  JobParseRequest      →  body sent by the caller
  JobParseResponse     →  body returned by the endpoint
  ParsedJobDescription →  the structured extraction result
  BatchParseRequest    →  up to 10 descriptions in one call
  BatchParseResponse   →  list of JobParseResponse + aggregate stats

  CLAUDE_JOB_PARSER_TOOL  Claude tool definition for structured JSON extraction
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator


# ══════════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════════

class WorkMode(StrEnum):
    REMOTE        = "remote"
    HYBRID        = "hybrid"
    ONSITE        = "onsite"
    NOT_SPECIFIED = "not_specified"


class EmploymentType(StrEnum):
    CONTRACT      = "contract"
    PERMANENT     = "permanent"
    PART_TIME     = "part_time"
    FREELANCE     = "freelance"
    INTERNSHIP    = "internship"
    NOT_SPECIFIED = "not_specified"


class SalaryPeriod(StrEnum):
    HOURLY  = "hourly"
    DAILY   = "daily"
    WEEKLY  = "weekly"
    MONTHLY = "monthly"
    ANNUAL  = "annual"


# ══════════════════════════════════════════════════════════════════════════════
# Sub-models
# ══════════════════════════════════════════════════════════════════════════════

class SalaryRange(BaseModel):
    """Structured pay information extracted from the job description."""

    min_value: float | None = Field(None, ge=0, description="Lower bound of the pay range")
    max_value: float | None = Field(None, ge=0, description="Upper bound of the pay range")
    currency:  str          = Field("USD",       description="ISO-4217 code, e.g. GBP, USD, EUR")
    period:    SalaryPeriod = Field(SalaryPeriod.ANNUAL, description="Pay frequency")
    raw_text:  str | None   = Field(None,        description="Verbatim salary text from the JD")

    @field_validator("currency", mode="before")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return (v or "USD").upper().strip()

    @model_validator(mode="after")
    def ensure_min_lte_max(self) -> "SalaryRange":
        """Swap silently when Gemini returns min/max reversed."""
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            self.min_value, self.max_value = self.max_value, self.min_value
        return self


class SkillSet(BaseModel):
    """Skills partitioned by importance tier."""

    required:  list[str] = Field(default_factory=list, description="Must-have skills")
    preferred: list[str] = Field(default_factory=list, description="Nice-to-have / bonus skills")

    @property
    def all_skills(self) -> list[str]:
        """Deduplicated union of required + preferred, order preserved."""
        seen: set[str] = set()
        out:  list[str] = []
        for s in self.required + self.preferred:
            k = s.lower()
            if k not in seen:
                seen.add(k)
                out.append(s)
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Core result
# ══════════════════════════════════════════════════════════════════════════════

class ParsedJobDescription(BaseModel):
    """
    Every field Gemini can extract from a raw job description.

    Fields are `None` / empty list when absent — never fabricated.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    job_title:    str | None = Field(None, description="Stated or inferred job title")
    company_name: str | None = Field(None, description="Hiring company, if mentioned")

    # ── Core four ─────────────────────────────────────────────────────────────
    skills:      SkillSet           = Field(default_factory=SkillSet)
    location:    str | None         = Field(None, description="City, country, or 'Remote'")
    salary:      SalaryRange | None = None
    work_mode:   WorkMode           = Field(WorkMode.NOT_SPECIFIED)

    # ── Employment ────────────────────────────────────────────────────────────
    employment_type:       EmploymentType = Field(EmploymentType.NOT_SPECIFIED)
    experience_years_min:  int | None     = Field(None, ge=0)
    experience_years_max:  int | None     = Field(None, ge=0)
    education_requirement: str | None     = None
    languages:             list[str]      = Field(default_factory=list)
    benefits:              list[str]      = Field(default_factory=list)

    # ── Quality ───────────────────────────────────────────────────────────────
    confidence_score:  float      = Field(0.0, ge=0.0, le=1.0,
                                          description="0–1 extraction completeness score")
    extraction_notes:  str | None = Field(None, description="Caveats noted by the model")

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_title":    "Senior Java Developer",
                "company_name": "Barclays",
                "skills": {
                    "required":  ["Java 17", "Spring Boot 3", "REST APIs", "AWS"],
                    "preferred": ["Kafka", "Kubernetes", "Terraform"],
                },
                "location":  "London, UK",
                "salary": {
                    "min_value": 500, "max_value": 650,
                    "currency": "GBP", "period": "daily",
                    "raw_text": "£500–£650/day",
                },
                "work_mode":              "hybrid",
                "employment_type":        "contract",
                "experience_years_min":   5,
                "experience_years_max":   None,
                "education_requirement":  None,
                "languages":              [],
                "benefits":               ["25 days holiday", "pension"],
                "confidence_score":       0.93,
                "extraction_notes":       None,
            }
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# Request / response wrappers
# ══════════════════════════════════════════════════════════════════════════════

# Reusable constraint for a single description string
_DescStr = Annotated[
    str,
    StringConstraints(min_length=50, max_length=20_000, strip_whitespace=True),
]


class JobParseRequest(BaseModel):
    """POST /parse request body."""

    description: _DescStr = Field(
        ...,
        description="Raw job description text (50–20 000 characters)",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "description": (
                    "Senior Java Developer — 6-month contract, London (hybrid 2 days/week).\n"
                    "Rate: £550–£650/day outside IR35.\n\n"
                    "Required: Java 17, Spring Boot 3, Microservices, REST APIs, PostgreSQL, Docker.\n"
                    "Nice to have: Kafka, Kubernetes, AWS, Terraform.\n\n"
                    "5+ years experience. Benefits: 25 days holiday, pension, private health."
                )
            }
        }
    }


class JobParseResponse(BaseModel):
    """POST /parse response body."""

    parsed:             ParsedJobDescription
    model_used:         str        = Field(..., description="Gemini model ID")
    input_chars:        int        = Field(..., description="Length of submitted description")
    total_tokens:       int | None = Field(None, description="Total tokens used")
    processing_time_ms: float      = Field(..., description="Gemini round-trip in ms")


class BatchParseRequest(BaseModel):
    """POST /parse/batch request body — 1 to 10 descriptions."""

    descriptions: list[_DescStr] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="1–10 raw job descriptions",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "descriptions": [
                    "Senior Java Developer, contract, London, £550/day, hybrid …",
                    "Python Backend Engineer, remote, $120k/yr, permanent …",
                ]
            }
        }
    }


class BatchParseResponse(BaseModel):
    """POST /parse/batch response body."""

    results:            list[JobParseResponse]
    total:              int
    total_tokens:       int | None = None
    processing_time_ms: float


# ══════════════════════════════════════════════════════════════════════════════
# Generic response envelope   {"data": …, "message": "…"}
# ══════════════════════════════════════════════════════════════════════════════

from typing import Generic, TypeVar  # noqa: E402

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    data:    T
    message: str = "success"


# ══════════════════════════════════════════════════════════════════════════════
# Claude tool definition
# Passed to client.messages.create(tools=[…], tool_choice={…}).
# Claude uses tool_use to return guaranteed-structured JSON — equivalent to
# Gemini's response_schema enforcement.
# ══════════════════════════════════════════════════════════════════════════════

CLAUDE_JOB_PARSER_TOOL: dict = {
    "name": "extract_job_info",
    "description": (
        "Extract structured information from a raw job description. "
        "Only extract what is explicitly stated or strongly implied — never fabricate data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            # Identity
            "job_title":             {"type": "string",  "description": "Stated or inferred job title"},
            "company_name":          {"type": "string",  "description": "Hiring company name, if mentioned"},
            # Skills
            "required_skills":       {"type": "array",   "items": {"type": "string"},
                                      "description": "Must-have technical and soft skills (normalised)"},
            "preferred_skills":      {"type": "array",   "items": {"type": "string"},
                                      "description": "Nice-to-have / bonus skills"},
            # Location & work mode
            "location":              {"type": "string",  "description": "City, country, or null if remote-only"},
            "work_mode":             {"type": "string",
                                      "enum": ["remote", "hybrid", "onsite", "not_specified"]},
            # Salary
            "salary_min":            {"type": "number",  "description": "Lower bound of pay range"},
            "salary_max":            {"type": "number",  "description": "Upper bound of pay range"},
            "salary_currency":       {"type": "string",  "description": "ISO-4217 currency code"},
            "salary_period":         {"type": "string",
                                      "enum": ["hourly", "daily", "weekly", "monthly", "annual"]},
            "salary_raw_text":       {"type": "string",  "description": "Verbatim salary text from the JD"},
            # Employment
            "employment_type":       {"type": "string",
                                      "enum": ["contract", "permanent", "part_time",
                                               "freelance", "internship", "not_specified"]},
            "experience_years_min":  {"type": "integer", "description": "Minimum years of experience required"},
            "experience_years_max":  {"type": "integer", "description": "Maximum years of experience required"},
            "education_requirement": {"type": "string",  "description": "Degree or certification requirement"},
            "languages":             {"type": "array",   "items": {"type": "string"},
                                      "description": "Spoken/written language requirements"},
            "benefits":              {"type": "array",   "items": {"type": "string"},
                                      "description": "Listed perks and benefits"},
            # Meta
            "confidence_score":      {"type": "number",
                                      "description": "0–1 completeness score: 1.0=all fields found, 0.0=no data"},
            "extraction_notes":      {"type": "string",
                                      "description": "Caveats or ambiguities noted during extraction"},
        },
        "required": [
            "required_skills",
            "preferred_skills",
            "work_mode",
            "employment_type",
            "confidence_score",
        ],
    },
}

# Backward-compatible alias — existing code that imports GEMINI_RESPONSE_SCHEMA continues to work.
GEMINI_RESPONSE_SCHEMA = CLAUDE_JOB_PARSER_TOOL
