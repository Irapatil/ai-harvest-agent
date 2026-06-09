"""
Pydantic models for job-description parsing.

Layers
──────
  JobParseRequest        →  what the caller sends
  JobParseResponse       →  what the endpoint returns
  ParsedJobDescription   →  the structured extraction (nested inside the response)
  GEMINI_RESPONSE_SCHEMA →  flat dict handed to Gemini's response_schema parameter
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


class Currency(StrEnum):
    """Common ISO-4217 codes; falls back to raw string for others."""
    GBP = "GBP"
    USD = "USD"
    EUR = "EUR"
    CAD = "CAD"
    AUD = "AUD"


# ══════════════════════════════════════════════════════════════════════════════
# Sub-models
# ══════════════════════════════════════════════════════════════════════════════

class SalaryRange(BaseModel):
    """Extracted salary / rate information."""

    min_value: float | None = Field(None, description="Lower bound of the pay range", ge=0)
    max_value: float | None = Field(None, description="Upper bound of the pay range", ge=0)
    currency:  str          = Field("USD", description="ISO 4217 code, e.g. GBP, USD, EUR")
    period:    SalaryPeriod = Field(SalaryPeriod.ANNUAL, description="Pay frequency")
    raw_text:  str | None   = Field(None, description="Verbatim salary text from the JD")

    @field_validator("currency", mode="before")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return (v or "USD").upper().strip()

    @model_validator(mode="after")
    def max_gte_min(self) -> "SalaryRange":
        """Swap min/max if they were provided in the wrong order."""
        if self.min_value is not None and self.max_value is not None:
            if self.min_value > self.max_value:
                self.min_value, self.max_value = self.max_value, self.min_value
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "min_value": 450,
                "max_value": 600,
                "currency": "GBP",
                "period": "daily",
                "raw_text": "£450–£600 per day",
            }
        }
    }


class SkillSet(BaseModel):
    """Skills split by importance tier."""

    required:  list[str] = Field(default_factory=list, description="Must-have skills")
    preferred: list[str] = Field(default_factory=list, description="Nice-to-have / bonus skills")

    @property
    def all_skills(self) -> list[str]:
        """Deduplicated union of required + preferred (order preserved)."""
        seen: set[str] = set()
        out: list[str] = []
        for s in self.required + self.preferred:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                out.append(s)
        return out

    model_config = {
        "json_schema_extra": {
            "example": {
                "required":  ["Java 17", "Spring Boot", "Microservices", "REST APIs"],
                "preferred": ["Kubernetes", "Kafka", "AWS"],
            }
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# Core parsed result
# ══════════════════════════════════════════════════════════════════════════════

class ParsedJobDescription(BaseModel):
    """
    Every field extracted from a raw job description.
    Fields are None / empty-list when the information is absent — never fabricated.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    job_title:    str | None = Field(None, description="Inferred or stated job title")
    company_name: str | None = Field(None, description="Hiring company, if mentioned")

    # ── The four primary fields requested ────────────────────────────────────
    skills:       SkillSet            = Field(default_factory=SkillSet)
    location:     str | None          = Field(None, description="City, country, or 'Remote'")
    salary:       SalaryRange | None  = None
    work_mode:    WorkMode            = Field(WorkMode.NOT_SPECIFIED)

    # ── Employment ────────────────────────────────────────────────────────────
    employment_type:       EmploymentType = Field(EmploymentType.NOT_SPECIFIED)
    experience_years_min:  int | None     = Field(None, ge=0, description="Minimum years of experience required")
    experience_years_max:  int | None     = Field(None, ge=0, description="Maximum years of experience required")
    education_requirement: str | None     = Field(None, description="e.g. 'Bachelor's in Computer Science'")
    languages:             list[str]      = Field(default_factory=list, description="Spoken/written language requirements")
    benefits:              list[str]      = Field(default_factory=list, description="Listed perks and benefits")

    # ── Meta ──────────────────────────────────────────────────────────────────
    confidence_score:  float       = Field(0.0, ge=0.0, le=1.0,
                                           description="0–1 completeness score assigned by Gemini")
    extraction_notes:  str | None  = Field(None, description="Caveats or ambiguities noted by the model")

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_title":    "Senior Java Developer",
                "company_name": "Barclays",
                "skills": {
                    "required":  ["Java 17", "Spring Boot", "AWS", "CI/CD"],
                    "preferred": ["Kafka", "Kubernetes"],
                },
                "location":  "London, UK",
                "salary": {
                    "min_value": 500, "max_value": 650,
                    "currency": "GBP", "period": "daily",
                    "raw_text": "£500–£650/day",
                },
                "work_mode":       "hybrid",
                "employment_type": "contract",
                "experience_years_min": 5,
                "experience_years_max": None,
                "confidence_score": 0.92,
                "extraction_notes": None,
            }
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# API request / response wrappers
# ══════════════════════════════════════════════════════════════════════════════

class JobParseRequest(BaseModel):
    """Body accepted by POST /api/v1/jobs/parse."""

    description: Annotated[
        str,
        StringConstraints(min_length=50, max_length=20_000, strip_whitespace=True),
    ] = Field(
        ...,
        description="Raw job description text (50–20 000 characters)",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "description": (
                    "We are looking for a Senior Java Developer on a 6-month contract "
                    "based in London (hybrid, 2 days in office). Rate: £550–£650/day.\n\n"
                    "Required: Java 17, Spring Boot 3, REST APIs, PostgreSQL, Docker.\n"
                    "Nice to have: Kafka, Kubernetes, AWS."
                )
            }
        }
    }


class JobParseResponse(BaseModel):
    """Body returned by POST /api/v1/jobs/parse."""

    parsed:              ParsedJobDescription
    model_used:          str   = Field(..., description="Gemini model ID that produced this result")
    input_chars:         int   = Field(..., description="Character count of the submitted description")
    total_tokens:        int | None = Field(None, description="Total tokens consumed (prompt + completion)")
    processing_time_ms:  float = Field(..., description="Wall-clock time for the Gemini round-trip")

    model_config = {
        "json_schema_extra": {
            "example": {
                "parsed": ParsedJobDescription.model_config["json_schema_extra"]["example"],
                "model_used": "gemini-2.0-flash",
                "input_chars": 412,
                "total_tokens": 820,
                "processing_time_ms": 1350.4,
            }
        }
    }


class BatchParseRequest(BaseModel):
    """Body for POST /api/v1/jobs/parse/batch (up to 10 descriptions)."""

    descriptions: list[
        Annotated[str, StringConstraints(min_length=50, max_length=20_000, strip_whitespace=True)]
    ] = Field(..., min_length=1, max_length=10, description="1–10 raw job descriptions")

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
    """Body returned by POST /api/v1/jobs/parse/batch."""

    results:             list[JobParseResponse]
    total:               int
    total_tokens:        int | None = None
    processing_time_ms:  float


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
