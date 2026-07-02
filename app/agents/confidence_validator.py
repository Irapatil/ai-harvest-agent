"""
Confidence Validation Agent — validates lead data and assigns confidence scores.

Scoring model
─────────────
Each field contributes a weighted score:

  LinkedIn profile URL  +0.30  (base identity anchor)
  Official email found  +0.25  (PUBLIC) / +0.35 (VERIFIED)
  Contact number found  +0.15  (PUBLIC) / +0.25 (VERIFIED)
  Company identified    +0.05
  Employment history    +0.05

Bands
  High    ≥ 0.80
  Medium  ≥ 0.60
  Low     < 0.60

Validation rules (data integrity)
──────────────────────────────────
• Email format validated via regex before accepting.
• Phone: must match Indian mobile pattern or E.164 international.
• LinkedIn URL: must match linkedin.com/in/ pattern.
• No fabricated data is ever written — invalid field is blanked rather
  than kept with a wrong value.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog

from app.models.lead_models import LeadRecord

logger = structlog.get_logger(__name__)

# ── Validation patterns ────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r'\A[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\Z'
)
_PHONE_RE = re.compile(
    r'^(?:(?:\+91|0091|91)?[6-9]\d{9}|\+[1-9]\d{6,14})$'
)
_LINKEDIN_RE = re.compile(
    r'https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?'
)

# ── Scoring weights ────────────────────────────────────────────────────────────
_W_LINKEDIN_URL  = 0.30
_W_EMAIL_VERIFIED = 0.35
_W_EMAIL_PUBLIC   = 0.25
_W_PHONE_VERIFIED = 0.25
_W_PHONE_PUBLIC   = 0.15
_W_COMPANY        = 0.05
_W_EMPLOYMENT     = 0.05

_BAND_HIGH   = 0.80
_BAND_MEDIUM = 0.60


def _is_valid_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def _is_valid_phone(phone: str) -> bool:
    cleaned = re.sub(r"[\s\-\(\)]", "", phone)
    return bool(cleaned and _PHONE_RE.match(cleaned))


def _is_valid_linkedin(url: str) -> bool:
    return bool(url and _LINKEDIN_RE.match(url))


def _score(record: LeadRecord) -> float:
    s = 0.0

    if _is_valid_linkedin(record.linkedin_profile_url):
        s += _W_LINKEDIN_URL

    if record.official_email and _is_valid_email(record.official_email):
        if record.email_status == "VERIFIED":
            s += _W_EMAIL_VERIFIED
        else:
            s += _W_EMAIL_PUBLIC

    if record.contact_number and _is_valid_phone(record.contact_number):
        if record.phone_status == "VERIFIED":
            s += _W_PHONE_VERIFIED
        else:
            s += _W_PHONE_PUBLIC

    if record.company or record.current_company:
        s += _W_COMPANY

    if record.employment_history:
        s += _W_EMPLOYMENT

    return round(min(s, 1.0), 3)


def _band(score: float) -> str:
    if score >= _BAND_HIGH:
        return "High"
    if score >= _BAND_MEDIUM:
        return "Medium"
    return "Low"


# ══════════════════════════════════════════════════════════════════════════════
# Confidence Validator
# ══════════════════════════════════════════════════════════════════════════════

class ConfidenceValidator:
    """
    Validates field-level data quality and assigns confidence scores.

    validate_and_score() is the main entry point:
      1. Validates email / phone / LinkedIn URL format.
      2. Blanks invalid fields (rather than keeping wrong data).
      3. Computes weighted confidence score.
      4. Assigns confidence_score label and updates confidence_value.
    """

    def validate_and_score(self, record: LeadRecord) -> LeadRecord:
        """Validate data quality and assign confidence. Returns the mutated record."""
        record = self._validate_fields(record)
        score  = _score(record)
        band   = _band(score)

        record.confidence_value = score
        record.confidence_score = band  # type: ignore[assignment]
        record.last_verified    = datetime.now(timezone.utc).isoformat()
        record.crm_status       = "Ready"

        logger.info(
            "confidence_scored",
            recruiter      = record.recruiter_name,
            score          = score,
            band           = band,
            has_email      = bool(record.official_email),
            has_phone      = bool(record.contact_number),
            has_linkedin   = bool(record.linkedin_profile_url),
        )
        return record

    def validate_batch(
        self,
        records:             list[LeadRecord],
        minimum_confidence:  float = 0.0,
    ) -> list[LeadRecord]:
        """
        Validate and score all records, optionally filtering below minimum_confidence.
        Returns the filtered + scored list.
        """
        scored = [self.validate_and_score(r) for r in records]
        if minimum_confidence > 0.0:
            before  = len(scored)
            scored  = [r for r in scored if r.confidence_value >= minimum_confidence]
            logger.info(
                "confidence_filter_applied",
                minimum   = minimum_confidence,
                before    = before,
                after     = len(scored),
                filtered  = before - len(scored),
            )

        high   = sum(1 for r in scored if r.confidence_score == "High")
        medium = sum(1 for r in scored if r.confidence_score == "Medium")
        low    = sum(1 for r in scored if r.confidence_score == "Low")
        logger.info(
            "crm_dataset_created",
            total  = len(scored),
            high   = high,
            medium = medium,
            low    = low,
        )
        return scored

    # ── Field-level validation ─────────────────────────────────────────────────

    def _validate_fields(self, record: LeadRecord) -> LeadRecord:
        """Blank any field that fails format validation."""

        # Email
        if record.official_email:
            if _is_valid_email(record.official_email):
                # Preserve status
                pass
            else:
                logger.debug(
                    "invalid_email_blanked",
                    recruiter = record.recruiter_name,
                    email     = record.official_email,
                )
                record.official_email = ""
                record.email_status   = "NOT_FOUND"  # type: ignore[assignment]

        # Phone
        if record.contact_number:
            cleaned = re.sub(r"[\s\-\(\)]", "", record.contact_number)
            if _is_valid_phone(cleaned):
                record.contact_number = cleaned   # normalised form
            else:
                logger.debug(
                    "invalid_phone_blanked",
                    recruiter = record.recruiter_name,
                    phone     = record.contact_number,
                )
                record.contact_number = ""
                record.phone_status   = "NOT_FOUND"  # type: ignore[assignment]

        # LinkedIn URL
        if record.linkedin_profile_url and not _is_valid_linkedin(record.linkedin_profile_url):
            logger.debug(
                "invalid_linkedin_url_blanked",
                recruiter = record.recruiter_name,
                url       = record.linkedin_profile_url,
            )
            record.linkedin_profile_url = ""

        return record

    # ── Diagnostic helpers (for reporting) ────────────────────────────────────

    @staticmethod
    def summarise(records: list[LeadRecord]) -> dict[str, Any]:
        """Return a statistics summary over a list of validated records."""
        return {
            "total":                     len(records),
            "high_confidence":           sum(1 for r in records if r.confidence_score == "High"),
            "medium_confidence":         sum(1 for r in records if r.confidence_score == "Medium"),
            "low_confidence":            sum(1 for r in records if r.confidence_score == "Low"),
            "with_email":                sum(1 for r in records if r.official_email),
            "with_phone":                sum(1 for r in records if r.contact_number),
            "with_linkedin":             sum(1 for r in records if r.linkedin_profile_url),
            "verified_emails":           sum(1 for r in records if r.email_status == "VERIFIED"),
            "public_emails":             sum(1 for r in records if r.email_status == "PUBLIC"),
            "verified_phones":           sum(1 for r in records if r.phone_status == "VERIFIED"),
            "public_phones":             sum(1 for r in records if r.phone_status == "PUBLIC"),
        }
