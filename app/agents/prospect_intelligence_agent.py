"""
Prospect / Recruiter Intelligence Agent

Enrichment pipeline per recruiter
───────────────────────────────────
Step 1  Company website scraping       → VERIFIED email / phone
Step 2  LinkedIn profile visit         → PUBLIC email / phone (click Contact Info)
Step 3  Naukri cross-source search     → PUBLIC email / phone
Step 4  Hierarchy discovery            → DuckDuckGo TA/HR leadership search
Step 5  Confidence scoring             → High / Medium / Low

CRITICAL RULES
──────────────
• Email and phone are NEVER predicted or generated.
• Only data actually scraped from a public source is stored.
• email_status:  VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• phone_status:  VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• Uses PersistentBrowserManager — Chrome already authenticated, no login automation.
• Debug JSON saved per run to data/results/lead_intelligence/debug/
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.models.prospect_models import (
    ProspectIntelligenceResult,
    ProspectRecord,
    ProspectResult,
)

logger = structlog.get_logger(__name__)

_OUTPUT_DIR           = Path("data/results/lead_intelligence")
_DEBUG_DIR            = _OUTPUT_DIR / "debug"
_INTERMEDIATE_DIR     = _OUTPUT_DIR / "intermediate"
_INTERMEDIATE_EVERY   = 25
_DEFAULT_CONCURRENCY  = 2

# ── Email / phone regexes ──────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b')

_SKIP_EMAIL_DOMAINS = {
    "linkedin.com", "sentry.io", "example.com", "gmail.com", "yahoo.com",
    "hotmail.com", "noreply", "no-reply", "notifications", "support",
}

_PHONE_RE = re.compile(
    r'(?:\+91[\s\-]?\d{5}[\s\-]?\d{5}'           # +91 Indian mobile
    r'|\+\d{1,3}[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}'  # international
    r'|0\d{2,4}[\s\-]\d{6,8})'                   # Indian landline
)

# ── Known company → domain map ─────────────────────────────────────────────────

_KNOWN_DOMAINS: dict[str, str] = {
    "kiya ai":                        "kiya.ai",
    "fino payments bank":             "finobank.com",
    "fino":                           "finobank.com",
    "odessa":                         "odessa.io",
    "amadeus labs":                   "amadeus.com",
    "amadeus":                        "amadeus.com",
    "ness digital engineering":       "ness.com",
    "ness digital engg":              "ness.com",
    "fractal analytics":              "fractal.ai",
    "fractal":                        "fractal.ai",
    "lentra ai":                      "lentra.ai",
    "lentra":                         "lentra.ai",
    "perfios":                        "perfios.com",
    "oracle financial services software limited": "oracle.com",
    "oracle":                         "oracle.com",
    "mastek":                         "mastek.com",
    "ciklum":                         "ciklum.com",
    "hexaware":                       "hexaware.com",
    "e2 open":                        "e2open.com",
    "dxc technologies":               "dxc.com",
    "dxc":                            "dxc.com",
    "niyoto infotech":                "niyoto.com",
    "hashroot limited":               "hashroot.in",
    "hashroot":                       "hashroot.in",
    "collabera":                      "collabera.com",
    "m360 research":                  "m360research.com",
    "vlink inc":                      "vlinkinfo.com",
    "vlink":                          "vlinkinfo.com",
    "paytm":                          "paytm.com",
    "ema unlimited":                  "ema-unlimited.com",
    "awign":                          "awign.com",
    "hurix":                          "hurix.com",
    "ig group":                       "iggroup.com",
    "intuitive ai":                   "intuitive.ai",
    "nucleus software":               "nucleussoftware.com",
    "i merit":                        "imerit.net",
    "imerit":                         "imerit.net",
    "global foundries":               "globalfoundries.com",
    "global foundaries":              "globalfoundries.com",
    "publicis sapient":               "publicissapient.com",
    "coupang":                        "coupang.com",
    "global data plc":                "globaldata.com",
    "global data":                    "globaldata.com",
    "razorpay":                       "razorpay.com",
    "neilson iq":                     "nielseniq.com",
    "nielsen iq":                     "nielseniq.com",
    "guidewire software":             "guidewire.com",
    "guidewire":                      "guidewire.com",
    "linde":                          "linde.com",
    "meesho":                         "meesho.com",
    "adobe":                          "adobe.com",
    "morgan stanley":                 "morganstanley.com",
    "ybrant digital":                 "ybrantdigital.com",
    "trilogy":                        "trilogy.com",
    "the hartford":                   "thehartford.com",
    "triotree technologies":          "triotree.com",
    "apisero inc":                    "apisero.com",
    "apisero":                        "apisero.com",
    "nttdata":                        "nttdata.com",
    "ntt data":                       "nttdata.com",
    "hexagon":                        "hexagon.com",
    "cyient":                         "cyient.com",
    "ideagen":                        "ideagen.com",
    "fleetx.io":                      "fleetx.io",
    "fleetx":                         "fleetx.io",
    "vikram solar":                   "vikramsolar.com",
    "markets and markets":            "marketsandmarkets.com",
    "markets & markets":              "marketsandmarkets.com",
    "adani realty":                   "adanirealty.com",
    "adani reality":                  "adanirealty.com",
    "lpl financial":                  "lpl.com",
    "ibm":                            "ibm.com",
    "paychex":                        "paychex.com",
    "clearwater analytics":           "clearwateranalytics.com",
    "itc infotech":                   "itcinfotech.com",
}

# ── Department keyword map ─────────────────────────────────────────────────────

_DEPT_MAP: list[tuple[list[str], str]] = [
    (["talent acquisition", "ta ", "talent", "recruiter", "recruitment", "sourcing"], "Talent Acquisition"),
    (["human resources", "hr ", "hrbp", "people success", "people & culture", "people operations"], "Human Resources"),
    (["chro", "chief people", "chief hr"], "HR Leadership"),
    (["technology", "engineering", "software", "tech"], "Technology"),
    (["finance", "cfo", "accounts", "accounting"], "Finance"),
    (["marketing", "brand", "growth"], "Marketing"),
    (["sales", "business development", "bd ", "revenue", "account manager"], "Sales & BD"),
    (["operations", "ops "], "Operations"),
    (["legal", "compliance"], "Legal & Compliance"),
    (["vendor", "vendor management"], "Vendor Management"),
    (["director", "vp", "vice president", "svp", "evp", "managing director", "md"], "Leadership"),
    (["ceo", "founder", "coo", "cto"], "CXO"),
]

# ── Position level map ────────────────────────────────────────────────────────
# Ordered most-specific first so the first match wins.

_POSITION_LEVEL_MAP: list[tuple[list[str], str]] = [
    (["chro", "chief human resources officer", "chief people officer", "chief hr"], "CHRO"),
    (["founder", "co-founder", "co founder"], "Founder"),
    (["senior vice president", "svp", "executive vice president", "evp"], "VP"),
    (["vice president", "vp of", " vp "], "VP"),
    (["associate director"], "Associate Director"),
    (["director"], "Director"),
    (["head of", "head -", "head,", "head &"], "Head"),
    (["senior manager", "sr. manager", "sr manager", "senior talent acquisition manager"], "Senior Manager"),
    (["manager"], "Manager"),
    (["senior recruiter", "sr. recruiter", "sr recruiter", "senior talent acquisition specialist", "senior ta"], "Senior Recruiter"),
    (["talent acquisition specialist", "ta specialist", "talent acquisition associate"], "Talent Acquisition Specialist"),
    (["recruiter", "talent acquisition"], "Recruiter"),
]

# ── Hiring domain map ─────────────────────────────────────────────────────────

_HIRING_DOMAIN_MAP: list[tuple[list[str], str]] = [
    (["artificial intelligence", " ai ", "machine learning", " ml ", "deep learning",
       "nlp", "genai", "generative ai", "llm", "computer vision", "data scientist"], "AI/ML"),
    (["cloud", " aws ", " azure ", " gcp ", "google cloud", "devops", "kubernetes",
       "docker", "site reliability", " sre "], "Cloud/DevOps"),
    ([" sap "], "SAP"),
    (["java", "spring boot", "j2ee", "microservices"], "Java"),
    (["data engineer", "data engineering", "big data", " spark ", " kafka ",
       "databricks", "etl "], "Data Engineering"),
    (["data science", "data analyst", "business intelligence", " bi ",
       "analytics", "tableau", "power bi"], "Data & Analytics"),
    (["full stack", "fullstack", " react ", " angular ", "nodejs",
       "frontend developer", "backend developer"], "Full Stack"),
    (["cybersecurity", "information security", "infosec", "penetration test"], "Cybersecurity"),
    (["salesforce", " crm ", "servicenow"], "Salesforce/CRM"),
    (["embedded", "firmware", " iot ", "vlsi", "fpga"], "Embedded/IoT"),
    (["product manager", "product management", "product owner"], "Product Management"),
    (["fintech", "payments", "lending", "treasury", "banking technology"], "FinTech"),
    (["healthcare", "medtech", "pharma", "biotech"], "Healthcare"),
    (["mobile developer", "android developer", "ios developer", "react native", "flutter"], "Mobile"),
    (["quality assurance", "automation test", "selenium", " qa engineer"], "QA/Testing"),
    (["erp", "oracle ebs", "peoplesoft"], "ERP"),
    (["network engineer", " cisco ", "routing switching"], "Networking"),
    (["mainframe", "cobol", "as400"], "Mainframe"),
]

# Pages to try on a company website for contact details
_CONTACT_PATHS = ["/contact", "/contact-us", "/team", "/about/leadership", "/about", "/management"]

# Selectors to try for the LinkedIn "Contact info" modal trigger
_LI_CONTACT_SELECTORS = [
    'a[href*="overlay/contact-info"]',
    'a[id*="contact-info"]',
    'a[data-control-name*="contact"]',
    '[class*="contact-info"] a',
    'a.pv-top-card--list-bullet',
]

# Selectors for the contact modal content
_LI_MODAL_SELECTORS = [
    '.artdeco-modal__content',
    '.pv-contact-info__contact-type',
    '[class*="contact-info__contact-type"]',
    '.ci-phone', '.ci-email',
    '[data-view-name*="contact"]',
]


# ═══════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load_prospects(xlsx_path: str) -> list[ProspectRecord]:
    """Read prospects.xlsx with forward-fill company name."""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl required: pip install openpyxl")

    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"Prospects file not found: {xlsx_path}")

    wb        = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws        = wb.active
    all_rows  = list(ws.iter_rows(values_only=True))
    wb.close()

    if not all_rows:
        return []

    header     = []
    data_start = 0
    for i, row in enumerate(all_rows):
        non_empty = [c for c in row if c is not None and str(c).strip()]
        if len(non_empty) > 1:
            header     = [str(c).strip().lower() if c else "" for c in row]
            data_start = i + 1
            break

    if not header:
        return []

    def _find_col(*kws: str) -> int:
        for kw in kws:
            for i, h in enumerate(header):
                if kw in h:
                    return i
        return -1

    col_company  = _find_col("client name", "company name", "company", "client")
    col_person   = _find_col("poc name", "person name", "contact name", "poc", "person")
    col_desig    = _find_col("designation", "title", "role")
    col_linkedin = _find_col("linkedin", "profile url", "url", "profile")

    if col_person == -1:
        return []

    def _cell(row: tuple, col: int) -> str:
        if col < 0 or col >= len(row) or row[col] is None:
            return ""
        val = str(row[col]).strip()
        return "" if val.lower() in ("none", "n/a", "-") else val

    records:      list[ProspectRecord] = []
    last_company: str                  = ""

    for row_i, row in enumerate(all_rows[data_start:], start=data_start + 1):
        person = _cell(row, col_person)
        if not person:
            continue
        company = _cell(row, col_company)
        if company:
            last_company = company
        records.append(ProspectRecord(
            company_name      = last_company,
            person_name       = person,
            designation       = _cell(row, col_desig),
            existing_linkedin = _cell(row, col_linkedin),
            row_index         = row_i,
        ))

    logger.info("prospects_loaded", total=len(records), path=xlsx_path)
    return records


def _infer_company_domain(company_name: str) -> tuple[str, str]:
    """Return (domain, website_url). Checks _KNOWN_DOMAINS then heuristic."""
    slug = company_name.lower().strip()
    for k, domain in _KNOWN_DOMAINS.items():
        if k == slug or k in slug or slug in k:
            return domain, f"https://www.{domain}"
    cleaned = re.sub(
        r'\b(inc|ltd|pvt|llc|corp|limited|technologies|tech|solutions|services|'
        r'systems|group|global|digital|software|labs|ai|io|infotech|analytics|'
        r'financial|payments|bank|realty|reality|plc|research|engg|engineering)\b',
        "", slug,
    )
    cleaned = re.sub(r'[^a-z0-9]', "", cleaned)
    if cleaned:
        domain = f"{cleaned}.com"
        return domain, f"https://www.{domain}"
    return "", ""


def _infer_department(designation: str, headline: str = "") -> str:
    text = (designation + " " + headline).lower()
    for keywords, dept in _DEPT_MAP:
        if any(kw in text for kw in keywords):
            return dept
    return ""


def _classify_position_level(designation: str, headline: str = "") -> str:
    """Map designation / LinkedIn headline to a standardised seniority tier."""
    text = f" {designation} {headline} ".lower()
    for keywords, level in _POSITION_LEVEL_MAP:
        if any(kw in text for kw in keywords):
            return level
    return "NOT_FOUND"


def _classify_hiring_domain(designation: str, headline: str, job_titles: list[str]) -> str:
    """
    Classify the technology / functional domain the recruiter hires for.
    Uses designation + LinkedIn headline + job titles posted in the harvest run.
    Returns up to 3 comma-separated domains, or NOT_FOUND.
    """
    text = f" {designation} {headline} {' '.join(job_titles)} ".lower()
    found: list[str] = []
    seen:  set[str]  = set()
    for keywords, domain in _HIRING_DOMAIN_MAP:
        if domain not in seen and any(kw in text for kw in keywords):
            found.append(domain)
            seen.add(domain)
        if len(found) >= 3:
            break
    return ", ".join(found) if found else "NOT_FOUND"


async def _extract_linkedin_profile_metadata(page: Any) -> dict:
    """
    Scrape extended profile metadata from an already-loaded LinkedIn profile page.

    Called AFTER _extract_linkedin_contact_info (no extra navigation needed).

    Returns dict with keys:
        employment_type    — Full-time | Contract | Part-time | Internship
        years_in_company   — "3 yrs 6 mos" (current-role tenure from experience section)
        overall_experience — summed career duration across all experience items
        company_industry   — LinkedIn industry taxonomy string
        company_size       — standard LinkedIn employee-range band

    All fields default to NOT_FOUND.
    """
    out: dict[str, str] = {
        "employment_type":    "NOT_FOUND",
        "years_in_company":   "NOT_FOUND",
        "overall_experience": "NOT_FOUND",
        "company_industry":   "NOT_FOUND",
        "company_size":       "NOT_FOUND",
    }
    try:
        body = await page.inner_text("body")

        # Employment type (appears near the role title in experience section)
        emp_m = re.search(
            r'\b(Full[\-\s]?time|Part[\-\s]?time|Contract|Freelance|Self[\-\s]?employed|Internship|Apprenticeship)\b',
            body, re.I,
        )
        if emp_m:
            out["employment_type"] = re.sub(r'[\-\s]+', '-', emp_m.group(1)).title()

        # Years in current company — first "– Present" duration on the page
        cur_m = re.search(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[–\-]+\s*Present'
            r'\s*·\s*(\d+\s*yr?s?(?:\s+\d+\s*mos?)?|\d+\s*mos?)',
            body, re.I,
        )
        if cur_m:
            out["years_in_company"] = cur_m.group(1).strip()

        # Overall experience — sum all experience-item duration tokens
        total_months = 0
        for dm in re.finditer(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[–\-]+\s*'
            r'(?:Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})'
            r'\s*·\s*(\d+)\s*yr?s?(?:\s+(\d+)\s*mos?)?',
            body, re.I,
        ):
            total_months += int(dm.group(1)) * 12 + int(dm.group(2) or 0)
        if total_months > 0:
            yrs, mos = divmod(total_months, 12)
            out["overall_experience"] = f"{yrs} yrs {mos} mos" if mos else f"{yrs} yrs"

        # Company size — standard LinkedIn employee-range bands
        size_m = re.search(
            r'(10,001\+|5,001[–\-]10,000|1,001[–\-]5,000|501[–\-]1,000|'
            r'201[–\-]500|51[–\-]200|11[–\-]50|2[–\-]10)\s*employees?',
            body, re.I,
        )
        if size_m:
            out["company_size"] = size_m.group(0).strip()

        # Company industry — match LinkedIn taxonomy terms in page text
        _LINKEDIN_INDUSTRIES = [
            "IT Services and IT Consulting", "Software Development",
            "Technology, Information and Internet", "Financial Services",
            "Banking", "Insurance", "Staffing and Recruiting",
            "Human Resources Services", "Management Consulting",
            "Business Consulting and Services", "Computer and Network Security",
            "Semiconductor Manufacturing", "Telecommunications",
            "E-Learning", "Education Administration Programs",
            "Retail", "Manufacturing", "Automotive",
            "Hospitals and Health Care", "Pharmaceutical Manufacturing",
            "Biotechnology Research", "Oil and Gas", "Construction",
            "Real Estate", "Advertising Services", "Entertainment Providers",
            "Food and Beverage Services", "Airlines and Aviation",
            "Transportation, Logistics, Supply Chain and Storage",
            "Information Technology and Services",
        ]
        body_lower = body.lower()
        for industry in _LINKEDIN_INDUSTRIES:
            if industry.lower() in body_lower:
                out["company_industry"] = industry
                break

    except Exception as exc:
        logger.debug("linkedin_metadata_extract_failed", error=str(exc))

    return out


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _score_confidence(result: ProspectResult, company_name: str, designation: str) -> str:
    """
    High   — LinkedIn confirmed + VERIFIED or PUBLIC email + company match
    Medium — LinkedIn confirmed + company match (no email)
    Low    — No LinkedIn profile resolved
    """
    if not result.linkedin_url:
        return "Low"

    company_match = bool(
        result.company_domain
        or any(
            tok in _normalize_name(result.linkedin_headline)
            for tok in _normalize_name(company_name).split()
            if len(tok) > 3
        )
    )

    has_real_email = result.email_status in ("VERIFIED", "PUBLIC")

    if company_match and has_real_email:
        return "High"
    if company_match:
        return "Medium"
    return "Low"


def _extract_email_from_text(text: str, company_domain: str = "") -> str:
    """
    Extract a work email from arbitrary text.
    Preference order:
      1. Email matching company domain (most authoritative)
      2. Any non-generic professional email
    Returns "" if none found or all matches are system/platform emails.
    """
    best_domain_match = ""
    first_generic     = ""

    for m in _EMAIL_RE.finditer(text):
        candidate = m.group(1).lower()
        host      = candidate.split("@")[1] if "@" in candidate else ""

        # Skip known system/platform domains
        if any(s in host for s in _SKIP_EMAIL_DOMAINS):
            continue
        # Skip generic local-parts
        local = candidate.split("@")[0]
        if local in {"info", "contact", "hr", "careers", "support", "admin", "hello", "hello", "enquiry"}:
            continue

        if company_domain and host == company_domain:
            best_domain_match = candidate
            break   # best possible match

        if not first_generic:
            first_generic = candidate

    return best_domain_match or first_generic


def _extract_phone_from_text(text: str) -> str:
    m = _PHONE_RE.search(text)
    return re.sub(r'\s+', '', m.group(0)) if m else ""


# ── Persistence ────────────────────────────────────────────────────────────────

def _save_intermediate(results: list[ProspectResult], batch_num: int, run_id: str) -> None:
    try:
        _INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _INTERMEDIATE_DIR / f"{run_id}_batch_{batch_num:03d}.json"
        path.write_text(
            json.dumps({"batch": batch_num, "count": len(results),
                        "results": [r.to_dict() for r in results]},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("intermediate_saved", batch=batch_num, count=len(results))
    except Exception as exc:
        logger.warning("intermediate_save_failed", error=str(exc))


def _save_final_json(results: list[ProspectResult], run_id: str) -> str:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path    = _OUTPUT_DIR / f"{run_id}_lead_intelligence.json"
    payload = {
        "run_id":          run_id,
        "total":           len(results),
        "enriched":        sum(1 for r in results if r.linkedin_url or r.company_domain),
        "high":            sum(1 for r in results if r.confidence_score == "High"),
        "medium":          sum(1 for r in results if r.confidence_score == "Medium"),
        "low":             sum(1 for r in results if r.confidence_score == "Low"),
        "verified_emails": sum(1 for r in results if r.email_status == "VERIFIED"),
        "public_emails":   sum(1 for r in results if r.email_status == "PUBLIC"),
        "verified_phones": sum(1 for r in results if r.phone_status == "VERIFIED"),
        "public_phones":   sum(1 for r in results if r.phone_status == "PUBLIC"),
        "no_contact":      sum(
            1 for r in results
            if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
        ),
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path.resolve())


def _save_debug_json(results: list[ProspectResult], run_id: str) -> str:
    """
    Save per-recruiter diagnostic JSON to data/results/lead_intelligence/debug/.
    Records: profile_opened, contact_section_found, email_found, phone_found,
             hierarchy_found, audit_log, email_status, phone_status.
    """
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path    = _DEBUG_DIR / f"{run_id}_diagnostics.json"
    records = []
    for r in results:
        records.append({
            "person_name":          r.person_name,
            "company_name":         r.company_name,
            "linkedin_url":         r.linkedin_url,
            "email_status":         r.email_status,
            "phone_status":         r.phone_status,
            "confidence_score":     r.confidence_score,
            "profile_opened":        r.profile_opened,
            "contact_section_found": r.contact_section_found,
            "email_found":           r.email_found,
            "phone_found":           r.phone_found,
            "hierarchy_found":       r.hierarchy_found,
            "audit_log":            r.enrichment_audit,
        })
    path.write_text(
        json.dumps({"run_id": run_id, "total": len(records), "records": records},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("debug_json_saved", path=str(path))
    return str(path.resolve())


def _save_summary(run_id: str, result: "ProspectIntelligenceResult") -> None:
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = _OUTPUT_DIR / "lead_intelligence_summary.json"
        path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("summary_save_failed", error=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# Browser helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _ddg_search_raw(page: Any, query: str, timeout_ms: int = 12000) -> list[dict]:
    """Execute a DuckDuckGo HTML search. Returns [{href, title, snippet}]."""
    encoded = urllib.parse.quote(query)
    url     = f"https://html.duckduckgo.com/html/?q={encoded}"
    items:  list[dict] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(600)
        links = await page.query_selector_all("a.result__a")
        for link in links[:8]:
            raw_href = await link.get_attribute("href") or ""
            title    = (await link.inner_text()).strip()
            href     = raw_href
            if "uddg=" in href:
                m = re.search(r'uddg=([^&]+)', href)
                if m:
                    href = urllib.parse.unquote(m.group(1))
            snippet = ""
            try:
                result_el = await page.evaluate_handle(
                    "(el) => el.closest('.result, .result--default')", link
                )
                if result_el:
                    snip_el = await result_el.query_selector(".result__snippet")
                    if snip_el:
                        snippet = (await snip_el.inner_text()).strip()
            except Exception:
                pass
            items.append({"href": href, "title": title, "snippet": snippet})
    except Exception as exc:
        logger.debug("ddg_search_failed", query=query[:80], error=str(exc))
    return items


async def _ddg_linkedin_search(page: Any, person_name: str, company_name: str) -> dict:
    """
    Use DuckDuckGo HTML to find a person's LinkedIn /in/ profile URL.
    Returns {linkedin_url, name, headline}. Empty dict if not found.
    """
    query = f'site:linkedin.com/in/ "{person_name}" "{company_name}"'
    items = await _ddg_search_raw(page, query, timeout_ms=12000)

    for item in items:
        m = re.search(r'linkedin\.com/in/([a-zA-Z0-9\-_%]+)', item["href"])
        if not m:
            continue
        slug        = m.group(1).split("?")[0]
        profile_url = f"https://www.linkedin.com/in/{slug}"
        title_clean = item["title"].replace("| LinkedIn", "").strip()
        name        = ""
        headline    = ""
        if " - " in title_clean:
            parts    = title_clean.split(" - ", 1)
            name     = parts[0].strip()
            headline = parts[1].strip()
        else:
            name = title_clean
        return {"linkedin_url": profile_url, "name": name, "headline": headline}

    return {}


async def _extract_linkedin_contact_info(
    page: Any, linkedin_url: str, company_domain: str = ""
) -> dict:
    """
    Step 2: Visit a LinkedIn profile and extract publicly visible email / phone.

    Strategy
    ────────
    1. Navigate to profile page.
    2. Try to click the "Contact info" link to open the modal.
    3. Extract email / phone from modal text.
    4. Fall back to scanning the full page body if modal failed.

    Returns:
        {email, phone, profile_opened, contact_section_found, headline, location}
    """
    out = {
        "email":                "",
        "phone":                "",
        "profile_opened":        False,
        "contact_section_found": False,
        "headline":              "",
        "location":              "",
    }

    try:
        resp = await page.goto(linkedin_url, wait_until="domcontentloaded", timeout=20000)
        if not resp or resp.status >= 400:
            return out

        out["profile_opened"] = True
        await page.wait_for_timeout(2000)

        # ── Grab headline + location from profile ─────────────────────────────
        for sel in ['h2.text-heading-xlarge', '.pv-top-card--list h2', '.top-card-layout__headline']:
            try:
                el = await page.query_selector(sel)
                if el:
                    out["headline"] = (await el.inner_text()).strip().split("\n")[0]
                    break
            except Exception:
                pass

        for sel in ['.pv-top-card--list-bullet li:first-child', '.top-card-layout__first-subline']:
            try:
                el = await page.query_selector(sel)
                if el:
                    out["location"] = (await el.inner_text()).strip().split("\n")[0]
                    break
            except Exception:
                pass

        # ── Try to click "Contact info" link/button ───────────────────────────
        clicked = False
        for sel in _LI_CONTACT_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(1800)
                    clicked = True
                    break
            except Exception:
                pass

        if clicked:
            # Collect text from the contact modal
            modal_text = ""
            for sel in _LI_MODAL_SELECTORS:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els:
                        t = (await el.inner_text()).strip()
                        if t:
                            modal_text += t + "\n"
                except Exception:
                    pass

            if modal_text:
                out["contact_section_found"] = True
                e = _extract_email_from_text(modal_text, company_domain)
                p = _extract_phone_from_text(modal_text)
                if e:
                    out["email"] = e
                if p:
                    out["phone"] = p

        # ── Fallback: scan full rendered page text ────────────────────────────
        if not out["email"] or not out["phone"]:
            body = await page.inner_text("body")
            if not out["email"]:
                out["email"] = _extract_email_from_text(body, company_domain)
            if not out["phone"]:
                out["phone"] = _extract_phone_from_text(body)

    except Exception as exc:
        logger.debug("linkedin_contact_extract_failed", url=linkedin_url, error=str(exc))

    return out


async def _scrape_company_website_contacts(
    page: Any, base_url: str, person_name: str, domain: str
) -> dict:
    """
    Step 1: Scrape company website for a specific person's email and phone.

    Tries: /contact, /contact-us, /team, /about/leadership, /about, /management.

    Email acceptance rules (ordered by confidence):
      1. Email whose local-part contains a name token → most confident
      2. Any @domain email on a team/leadership page
      3. Skip generic local-parts (info, hr, contact, careers, etc.)

    Returns {email, phone, source_page}.
    """
    person_tokens = {t.lower() for t in re.split(r'\s+', person_name) if len(t) > 2}

    domain_re = (
        re.compile(rf'\b([a-zA-Z0-9._%+-]+@{re.escape(domain)})\b', re.I)
        if domain else None
    )

    _GENERIC_LOCALS = {"info", "contact", "hr", "careers", "support", "admin",
                       "hello", "enquiry", "recruitment", "jobs", "noreply"}

    for path in _CONTACT_PATHS:
        url = base_url.rstrip("/") + path
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=8000)
            if not resp or resp.status >= 400:
                continue
            await page.wait_for_timeout(400)
            text = await page.inner_text("body")
        except Exception:
            continue

        # Look for domain-matching email with name token (most confident)
        found_email = ""
        if domain_re:
            for m in domain_re.finditer(text):
                local = m.group(1).split("@")[0].lower()
                if local in _GENERIC_LOCALS:
                    continue
                if any(tok in local for tok in person_tokens):
                    found_email = m.group(1).lower()
                    break

        # Fallback: any non-generic domain email on team/leadership pages
        if not found_email and domain_re and path in ("/team", "/about/leadership", "/management"):
            for m in domain_re.finditer(text):
                local = m.group(1).split("@")[0].lower()
                if local not in _GENERIC_LOCALS:
                    found_email = m.group(1).lower()
                    break

        found_phone = _extract_phone_from_text(text)

        if found_email or found_phone:
            return {"email": found_email, "phone": found_phone, "source_page": path}

    return {"email": "", "phone": "", "source_page": ""}


async def _search_naukri_contact(
    page: Any, person_name: str, company_name: str, company_domain: str = ""
) -> dict:
    """
    Step 3: Search Naukri via DuckDuckGo for a recruiter profile.
    Visit the profile page if found and extract any public contact info.

    Returns {email, phone, profile_url}.
    """
    out = {"email": "", "phone": "", "profile_url": ""}

    query = f'site:naukri.com/mnjuser "{person_name}" "{company_name}"'
    items = await _ddg_search_raw(page, query, timeout_ms=10000)

    for item in items[:3]:
        href = item["href"]
        if "naukri.com" not in href:
            continue
        if "/mnjuser/" not in href and "/recruiterprofile/" not in href:
            continue

        out["profile_url"] = href
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(1000)
            body  = await page.inner_text("body")
            email = _extract_email_from_text(body, company_domain)
            phone = _extract_phone_from_text(body)
            if email:
                out["email"] = email
            if phone:
                out["phone"] = phone
        except Exception as exc:
            logger.debug("naukri_profile_visit_failed", url=href, error=str(exc))

        break  # first matching result is enough

    return out


async def _ddg_hierarchy_search(page: Any, company_name: str) -> str:
    """
    Step 4: Find TA/HR leadership at company via DuckDuckGo.
    Returns pipe-separated 'Name (Title)' string.
    """
    query = f'"Head of Talent Acquisition" OR "CHRO" OR "TA Head" "{company_name}" site:linkedin.com/in/'
    items = await _ddg_search_raw(page, query, timeout_ms=10000)

    found:      list[str] = []
    seen_names: set[str]  = set()

    for item in items[:4]:
        if "linkedin.com/in/" not in item["href"]:
            continue
        title_clean = item["title"].replace("| LinkedIn", "").strip()
        name  = ""
        role  = ""
        if " - " in title_clean:
            parts = title_clean.split(" - ", 1)
            name  = parts[0].strip()
            role  = parts[1].strip()[:60]
        else:
            name = title_clean

        if name and name.lower() not in seen_names:
            seen_names.add(name.lower())
            found.append(name + (f" ({role})" if role else ""))

    return " | ".join(found) if found else ""


# ═══════════════════════════════════════════════════════════════════════════════
# ProspectIntelligenceAgent
# ═══════════════════════════════════════════════════════════════════════════════

class ProspectIntelligenceAgent:
    """
    Orchestrates recruiter contact discovery for prospects from an Excel file.

    Usage::

        agent  = ProspectIntelligenceAgent(concurrency=2)
        result = await agent.run("data/prospects/input/prospects.xlsx")
    """

    def __init__(self, concurrency: int = _DEFAULT_CONCURRENCY) -> None:
        self._concurrency = max(1, min(concurrency, 5))

    async def run(self, xlsx_path: str) -> ProspectIntelligenceResult:
        started_at = datetime.now(timezone.utc)
        run_id     = started_at.strftime("%Y%m%d_%H%M%S")

        logger.info("prospect_intelligence_start", run_id=run_id, input=xlsx_path)

        prospects = _load_prospects(xlsx_path)
        if not prospects:
            completed_at = datetime.now(timezone.utc)
            return ProspectIntelligenceResult(
                run_id=run_id, started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(), runtime_minutes=0.0,
                total_prospects=0, enriched=0,
                high_confidence=0, medium_confidence=0, low_confidence=0,
                verified_emails=0, public_emails=0,
                verified_phones=0, public_phones=0, no_contact=0,
                json_path="", excel_path="",
            )

        all_results: list[ProspectResult] = []

        from app.services.config_service import ConfigService
        from app.scrapers.browser_manager import PersistentBrowserManager

        config = ConfigService().load()
        sem    = asyncio.Semaphore(self._concurrency)

        async def _enrich_one(prospect: ProspectRecord) -> ProspectResult:
            async with sem:
                page = await pbm.new_page()
                try:
                    return await self._enrich_prospect(page, prospect)
                except Exception as exc:
                    logger.warning("enrich_error", person=prospect.person_name, error=str(exc))
                    domain, website = _infer_company_domain(prospect.company_name)
                    return ProspectResult(
                        company_name     = prospect.company_name,
                        person_name      = prospect.person_name,
                        designation      = prospect.designation,
                        company_domain   = domain,
                        company_website  = website,
                        email_status     = "NOT_FOUND",
                        phone_status     = "NOT_FOUND",
                        department       = _infer_department(prospect.designation),
                        confidence_score = "Low",
                        source           = "Error",
                        enrichment_audit = [f"Exception: {exc}"],
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        async with PersistentBrowserManager(
            profile_dir = config.browser.chrome_profile,
            headless    = config.browser.headless,
            slow_mo     = config.browser.slow_mo_ms,
        ) as pbm:
            BATCH = _INTERMEDIATE_EVERY
            for batch_start in range(0, len(prospects), BATCH):
                batch         = prospects[batch_start: batch_start + BATCH]
                batch_tasks   = [_enrich_one(p) for p in batch]
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                for i, r in enumerate(batch_results):
                    if isinstance(r, Exception):
                        p               = batch[i]
                        domain, website = _infer_company_domain(p.company_name)
                        all_results.append(ProspectResult(
                            company_name     = p.company_name,
                            person_name      = p.person_name,
                            designation      = p.designation,
                            company_domain   = domain,
                            company_website  = website,
                            email_status     = "NOT_FOUND",
                            phone_status     = "NOT_FOUND",
                            department       = _infer_department(p.designation),
                            confidence_score = "Low",
                            source           = "Error",
                            enrichment_audit = [f"gather exception: {r}"],
                        ))
                    else:
                        all_results.append(r)  # type: ignore[arg-type]

                batch_num = batch_start // BATCH + 1
                _save_intermediate(all_results, batch_num, run_id)
                logger.info(
                    "batch_complete",
                    batch=batch_num, batch_count=len(batch),
                    total_done=len(all_results), total=len(prospects),
                )

        json_path  = _save_final_json(all_results, run_id)
        debug_path = _save_debug_json(all_results, run_id)
        excel_path = ""
        try:
            from app.services.prospect_excel_service import ProspectExcelService
            excel_path = ProspectExcelService().export(all_results, run_id)
        except Exception as exc:
            logger.warning("excel_export_failed", error=str(exc))

        completed_at    = datetime.now(timezone.utc)
        elapsed_seconds = (completed_at - started_at).total_seconds()

        result = ProspectIntelligenceResult(
            run_id            = run_id,
            started_at        = started_at.isoformat(),
            completed_at      = completed_at.isoformat(),
            runtime_minutes   = round(elapsed_seconds / 60, 1),
            total_prospects   = len(prospects),
            enriched          = sum(1 for r in all_results if r.linkedin_url or r.company_domain),
            high_confidence   = sum(1 for r in all_results if r.confidence_score == "High"),
            medium_confidence = sum(1 for r in all_results if r.confidence_score == "Medium"),
            low_confidence    = sum(1 for r in all_results if r.confidence_score == "Low"),
            verified_emails   = sum(1 for r in all_results if r.email_status == "VERIFIED"),
            public_emails     = sum(1 for r in all_results if r.email_status == "PUBLIC"),
            verified_phones   = sum(1 for r in all_results if r.phone_status == "VERIFIED"),
            public_phones     = sum(1 for r in all_results if r.phone_status == "PUBLIC"),
            no_contact        = sum(
                1 for r in all_results
                if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
            ),
            json_path  = json_path,
            excel_path = excel_path,
            results    = all_results,
        )

        _save_summary(run_id, result)

        logger.info(
            "prospect_intelligence_complete",
            run_id          = run_id,
            total           = result.total_prospects,
            verified_emails = result.verified_emails,
            public_emails   = result.public_emails,
            no_contact      = result.no_contact,
            runtime_minutes = result.runtime_minutes,
            debug_path      = debug_path,
        )
        return result

    # ── Per-record enrichment ──────────────────────────────────────────────────

    async def _enrich_prospect(self, page: Any, prospect: ProspectRecord) -> ProspectResult:
        """
        Full enrichment pipeline for one recruiter / prospect.

        Step 1 — Company website scraping   → VERIFIED
        Step 2 — LinkedIn profile visit     → PUBLIC
        Step 3 — Naukri cross-validation    → PUBLIC
        Step 4 — Hierarchy discovery
        Step 5 — Confidence scoring

        NO email or phone prediction. NOT_FOUND means not publicly available.
        """
        t0      = time.monotonic()
        result  = ProspectResult(
            company_name = prospect.company_name,
            person_name  = prospect.person_name,
            designation  = prospect.designation,
        )
        sources: list[str] = []
        audit:   list[str] = []

        logger.info("enriching", person=prospect.person_name, company=prospect.company_name)

        # ── Company domain ─────────────────────────────────────────────────────
        domain, website = _infer_company_domain(prospect.company_name)
        result.company_domain  = domain
        result.company_website = website

        # ══════════════════════════════════════════════════════════════════════
        # Step 1 — Company website scraping (VERIFIED)
        # ══════════════════════════════════════════════════════════════════════
        if website:
            try:
                contact = await _scrape_company_website_contacts(
                    page, website, prospect.person_name, domain
                )
                if contact["email"]:
                    result.official_email_id = contact["email"]
                    result.email_status      = "VERIFIED"
                    sources.append("Company Website")
                    audit.append(f"S1 VERIFIED email: {contact['email']} (page:{contact['source_page']})")
                else:
                    audit.append(f"S1 website scraped — no personal email found (domain:{domain})")
                if contact["phone"]:
                    result.contact_number = contact["phone"]
                    result.phone_status   = "VERIFIED"
                    sources.append("Company Website (Phone)")
                    audit.append(f"S1 VERIFIED phone: {contact['phone']}")
            except Exception as exc:
                audit.append(f"S1 error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 2 — LinkedIn profile visit (PUBLIC)
        # ══════════════════════════════════════════════════════════════════════
        try:
            linkedin_url = prospect.existing_linkedin

            if not linkedin_url:
                ddg = await _ddg_linkedin_search(page, prospect.person_name, prospect.company_name)
                await page.wait_for_timeout(600)
                linkedin_url             = ddg.get("linkedin_url", "")
                result.linkedin_headline = ddg.get("headline", "")
                if linkedin_url:
                    sources.append("DuckDuckGo")
                    audit.append(f"S2 LinkedIn URL found via DDG: {linkedin_url}")
                else:
                    audit.append("S2 DDG LinkedIn search: no profile found")

            if linkedin_url:
                result.linkedin_url = linkedin_url
                contact_info = await _extract_linkedin_contact_info(page, linkedin_url, domain)

                result.profile_opened        = contact_info["profile_opened"]
                result.contact_section_found = contact_info["contact_section_found"]

                if contact_info["headline"] and not result.linkedin_headline:
                    result.linkedin_headline = contact_info["headline"]
                if contact_info["location"]:
                    result.location = contact_info["location"]

                if contact_info["email"] and result.email_status != "VERIFIED":
                    result.official_email_id = contact_info["email"]
                    result.email_status      = "PUBLIC"
                    sources.append("LinkedIn Profile")
                    audit.append(f"S2 PUBLIC email from LinkedIn: {contact_info['email']}")
                else:
                    audit.append(
                        f"S2 LinkedIn visited — no email found "
                        f"(contact_section:{contact_info['contact_section_found']})"
                    )

                if contact_info["phone"] and result.phone_status != "VERIFIED":
                    result.contact_number = contact_info["phone"]
                    result.phone_status   = "PUBLIC"
                    sources.append("LinkedIn Profile (Phone)")
                    audit.append(f"S2 PUBLIC phone from LinkedIn: {contact_info['phone']}")

                # Extended metadata — scraped from the already-loaded profile page
                if result.profile_opened:
                    try:
                        meta = await _extract_linkedin_profile_metadata(page)
                        if meta["employment_type"] != "NOT_FOUND":
                            result.employment_type = meta["employment_type"]
                        if meta["years_in_company"] != "NOT_FOUND":
                            result.years_in_company = meta["years_in_company"]
                        if meta["overall_experience"] != "NOT_FOUND":
                            result.overall_experience = meta["overall_experience"]
                        if meta["company_industry"] != "NOT_FOUND":
                            result.company_industry = meta["company_industry"]
                        if meta["company_size"] != "NOT_FOUND":
                            result.company_size = meta["company_size"]
                        audit.append(
                            f"S2 metadata: emp_type={meta['employment_type']}, "
                            f"tenure={meta['years_in_company']}, "
                            f"industry={meta['company_industry']}, "
                            f"size={meta['company_size']}"
                        )
                    except Exception as exc:
                        audit.append(f"S2 metadata error: {exc}")

        except Exception as exc:
            audit.append(f"S2 error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 3 — Naukri cross-source (PUBLIC)
        # ══════════════════════════════════════════════════════════════════════
        if result.email_status == "NOT_FOUND" or result.phone_status == "NOT_FOUND":
            try:
                naukri = await _search_naukri_contact(
                    page, prospect.person_name, prospect.company_name, domain
                )
                if naukri["profile_url"]:
                    sources.append("Naukri")
                    audit.append(f"S3 Naukri profile found: {naukri['profile_url']}")
                if naukri["email"] and result.email_status == "NOT_FOUND":
                    result.official_email_id = naukri["email"]
                    result.email_status      = "PUBLIC"
                    audit.append(f"S3 PUBLIC email from Naukri: {naukri['email']}")
                if naukri["phone"] and result.phone_status == "NOT_FOUND":
                    result.contact_number = naukri["phone"]
                    result.phone_status   = "PUBLIC"
                    audit.append(f"S3 PUBLIC phone from Naukri: {naukri['phone']}")
                if not naukri["profile_url"]:
                    audit.append("S3 Naukri: no profile found")
            except Exception as exc:
                audit.append(f"S3 Naukri error: {exc}")

        # ── Update email_found / phone_found diagnostics ───────────────────────
        result.email_found = result.email_status in ("VERIFIED", "PUBLIC")
        result.phone_found = result.phone_status in ("VERIFIED", "PUBLIC")

        # ══════════════════════════════════════════════════════════════════════
        # Step 4 — Hierarchy discovery (DuckDuckGo)
        # ══════════════════════════════════════════════════════════════════════
        if prospect.company_name:
            try:
                hierarchy = await _ddg_hierarchy_search(page, prospect.company_name)
                result.reporting_hierarchy = hierarchy
                result.hierarchy_found     = bool(hierarchy)
                if hierarchy:
                    sources.append("DDG Hierarchy")
                    audit.append(f"S4 Hierarchy: {hierarchy[:100]}")
                else:
                    audit.append("S4 Hierarchy: none found")
            except Exception as exc:
                audit.append(f"S4 hierarchy error: {exc}")

        # ── Department inference ────────────────────────────────────────────────
        result.department = _infer_department(prospect.designation, result.linkedin_headline)

        # ── Position level & hiring domain (deterministic classifiers) ──────────
        result.position_level = _classify_position_level(prospect.designation, result.linkedin_headline)
        result.hiring_domain  = _classify_hiring_domain(prospect.designation, result.linkedin_headline, [])

        # ── Confidence scoring ─────────────────────────────────────────────────
        result.confidence_score = _score_confidence(
            result, prospect.company_name, prospect.designation
        )
        result.source           = ", ".join(dict.fromkeys(sources))
        result.enrichment_audit = audit

        elapsed = round(time.monotonic() - t0, 1)
        logger.info(
            "enriched",
            person       = prospect.person_name,
            linkedin     = bool(result.linkedin_url),
            email_status = result.email_status,
            phone_status = result.phone_status,
            confidence   = result.confidence_score,
            duration_s   = elapsed,
        )
        return result
