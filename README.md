# AI Harvest Agent

An enterprise-grade job intelligence platform built with **FastAPI** and **Playwright** that harvests job postings from LinkedIn, Naukri, and Dice, then enriches recruiter contact records through multi-level discovery — producing a verified Lead Intelligence report with 19 fields per recruiter.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Architecture](#architecture)
- [Folder Structure](#folder-structure)
- [Technologies Used](#technologies-used)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [Running the Server](#running-the-server)
- [Swagger / API Explorer](#swagger--api-explorer)
- [API Endpoints](#api-endpoints)
- [Harvest Workflow](#harvest-workflow)
- [Lead Intelligence Workflow](#lead-intelligence-workflow)
- [Configuration](#configuration)
- [Data Integrity Policy](#data-integrity-policy)

---

## Project Overview

The AI Harvest Agent performs two primary operations:

1. **Job Harvest** — Scrapes LinkedIn, Naukri, and Dice in parallel for job postings matching a configured keyword and location (default: `AI Engineer`, `India`). Applies business filters to classify each posting as Direct Client, GCC, Staffing Firm, or Ambiguous. Saves a combined JSON + Excel report.

2. **Lead Intelligence** — Takes the harvested recruiter/job-poster records and enriches each one through a 3-level contact discovery pipeline:
   - **Level 1 (VERIFIED):** Company website scraping (`/contact`, `/team`, `/about`)
   - **Level 2 (PUBLIC):** LinkedIn profile → Contact Info modal
   - **Level 3 (PUBLIC):** Naukri recruiter profile via DuckDuckGo search

Output: a 19-column Excel report per recruiter with verified contact details, seniority tier, hiring domain, company metadata, and confidence score.

**Key principle:** No data is fabricated. Emails and phone numbers are only populated from scraped public sources — never predicted or generated.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        FastAPI Server                        │
│                     http://localhost:8001                    │
├──────────────────────────┬──────────────────────────────────┤
│     Harvest Pipeline     │     Lead Intelligence Pipeline    │
│                          │                                   │
│  LinkedIn Agent ─────┐   │   Recruiter Contact Agent         │
│  Naukri Agent ───────┼──►│   Prospect Intelligence Agent     │
│  Dice Agent ─────────┘   │   3-level contact discovery       │
│      │                   │          │                        │
│  Orchestrator Agent       │   Excel Export (19 columns)       │
│      │                   │                                   │
│  Business Filter Service  │   DuckDuckGo Search + LinkedIn    │
│  (Direct Client/GCC/      │   Contact Info + Naukri cross-    │
│   Staffing/Ambiguous)     │   reference                       │
└──────────────────────────┴──────────────────────────────────┘
         │                              │
   data/results/combined/        data/results/lead_intelligence/
   *_combined.json               *_Lead_Intelligence_Report.xlsx
   *_harvest.xlsx                *_recruiter_contacts.json
```

---

## Folder Structure

```
ai-harvest-agent/
├── app/
│   ├── main.py                          ← FastAPI application factory
│   ├── config.py                        ← Settings (pydantic-settings)
│   ├── agents/
│   │   ├── base_agent.py                ← Abstract agent base class
│   │   ├── orchestrator_agent.py        ← Unified harvest orchestrator
│   │   ├── linkedin_agent.py            ← LinkedIn job scraper
│   │   ├── naukri_agent.py              ← Naukri job scraper
│   │   ├── dice_agent.py                ← Dice job scraper
│   │   ├── recruiter_contact_agent.py   ← Contact discovery & enrichment
│   │   └── prospect_intelligence_agent.py ← Lead intelligence pipeline
│   ├── scrapers/
│   │   ├── browser_manager.py           ← Persistent Chrome profile manager
│   │   ├── linkedin_scraper.py          ← LinkedIn Playwright scraper
│   │   ├── dice_scraper.py              ← Dice SPA scraper
│   │   └── linkedin_jobs.py             ← LinkedIn job detail extractor
│   ├── services/
│   │   ├── config_service.py            ← harvest_config.json CRUD
│   │   ├── business_filter_service.py   ← Direct Client / GCC / Staffing classifier
│   │   ├── prospect_excel_service.py    ← 19-column Excel report generator
│   │   ├── excel_export_service.py      ← Harvest Excel generator
│   │   ├── lead_enrichment_service.py   ← Contact enrichment orchestration
│   │   ├── scheduler_service.py         ← APScheduler wrapper
│   │   ├── run_history_service.py       ← Run history persistence
│   │   └── playwright_service.py        ← Browser pool management
│   ├── models/
│   │   ├── unified_job.py               ← UnifiedJob dataclass (all sources)
│   │   ├── prospect_models.py           ← ProspectResult (19-field schema)
│   │   └── harvest_models.py            ← Harvest config/request models
│   ├── routes/
│   │   ├── run_harvest_agent.py         ← POST /run-harvest-agent (primary)
│   │   ├── harvest_routes.py            ← Config, results, schedule endpoints
│   │   ├── linkedin_routes.py           ← LinkedIn-specific endpoints
│   │   ├── naukri_routes.py             ← Naukri-specific endpoints
│   │   ├── dice_routes.py               ← Dice-specific endpoints
│   │   ├── recruiter_routes.py          ← POST /run-recruiter-discovery
│   │   └── prospect_routes.py           ← POST /run-prospect-intelligence
│   ├── prompts/                         ← LLM prompt templates
│   └── core/                            ← Middleware, exceptions, dependencies
├── data/
│   ├── config/
│   │   └── harvest_config.json          ← Active harvest configuration
│   ├── master/
│   │   ├── direct_client_master_list.json ← Direct client company list
│   │   ├── gcc_master_list.json           ← GCC company list
│   │   ├── staffing_firm_master_list.json ← Staffing firm list
│   │   ├── ambiguous_companies.json       ← Ambiguous company list
│   │   └── domain_keywords.json           ← Technology domain keywords
│   ├── results/                          ← Runtime output (git-ignored)
│   ├── sessions/                         ← Browser sessions (git-ignored)
│   ├── chrome_profile/                   ← Persistent Chrome profile (git-ignored)
│   └── prospects/input/                  ← Input Excel files (git-ignored)
├── job_parser_service/                   ← Optional standalone Gemini parser microservice
├── tests/                               ← Pytest test suites
├── scripts/                             ← Utility scripts
├── config/                              ← Job qualification configs
├── .env.example                         ← Environment variable template
├── requirements.txt                     ← Python dependencies
├── Dockerfile                           ← Container definition
├── docker-compose.yml                   ← Multi-service compose file
└── run.py                               ← Uvicorn launcher
```

---

## Technologies Used

| Technology | Purpose |
|---|---|
| **FastAPI** | REST API framework |
| **Playwright (async)** | Browser automation for LinkedIn, Naukri, Dice |
| **Pydantic v2** | Request/response validation and settings |
| **structlog** | Structured JSON logging |
| **APScheduler** | Scheduled harvest runs |
| **openpyxl** | Excel report generation |
| **Anthropic Claude** | LLM-assisted classification and enrichment |
| **Google Gemini** | Alternative LLM (job_parser_service) |
| **DuckDuckGo Search** | Contact discovery without API key |
| **asyncio + Semaphore** | Concurrent browser session management |

---

## Installation

### Prerequisites

- Python 3.11+
- Google Chrome (for persistent browser profile)
- Git

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/ai-harvest-agent.git
cd ai-harvest-agent

# 2. Create virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers (uses bundled Chromium)
playwright install chromium

# 5. Configure environment
copy .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum

# 6. Set up Chrome profile for scraping (one-time)
#    Open Chrome with the profile directory:
#    chrome.exe --user-data-dir="data\chrome_profile"
#    Log in to LinkedIn and Naukri manually, then close Chrome.
#    The agent will reuse these sessions automatically.
```

---

## Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic Claude API key (`sk-ant-...`) |
| `GEMINI_API_KEY` | No | Google Gemini API key (job_parser_service only) |
| `APP_SECRET_KEY` | Yes | 32+ char random string for internal signing |
| `FASTAPI_HOST` | No | Server bind host (default: `0.0.0.0`) |
| `FASTAPI_PORT` | No | Server port (default: `8001`) |
| `CORS_ORIGINS` | No | Allowed CORS origins (comma-separated) |
| `PLAYWRIGHT_HEADLESS` | No | Run browser headless (default: `true`) |
| `PLAYWRIGHT_POOL_SIZE` | No | Browser pool size (default: `3`) |
| `STORAGE_BACKEND` | No | `local` or `s3` (default: `local`) |

> **Authentication note:** LinkedIn and Naukri sessions are managed via a persistent Chrome profile at `data/chrome_profile/`. Users log in manually once — the agent reuses those sessions. No username/password automation.

---

## Running the Server

```bash
# Using the run script (recommended)
python run.py

# Or directly with uvicorn
.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8001

# PowerShell convenience scripts
.\run_harvest.ps1      # Fire a harvest run
.\run_prospect.ps1     # Fire a prospect intelligence run
```

The server starts at `http://localhost:8001`.

---

## Swagger / API Explorer

Interactive Swagger UI with Try It Out enabled:

```
http://localhost:8001/docs
```

OpenAPI JSON spec (for client code generation):

```
http://localhost:8001/openapi.json
```

---

## API Endpoints

All endpoints return HTTP 200. Errors are reported in the JSON body as `{ "status": "failed", ... }`.

### Harvest Agent

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/run-harvest-agent` | ★ Primary — trigger full harvest (all sources) |
| `POST` | `/run-harvest` | Alternate harvest trigger (legacy) |
| `GET` | `/run-history` | List all harvest run history entries |
| `GET` | `/run-history/{run_id}` | Single run history entry |
| `GET` | `/harvest-config` | Get current harvest configuration |
| `PUT` | `/harvest-config` | Update harvest configuration |
| `GET` | `/harvest-results` | List saved harvest result files |
| `GET` | `/harvest-results/{run_id}` | Full job list for one run |
| `GET` | `/harvest-schedule/status` | Scheduler status |
| `POST` | `/harvest-schedule/toggle` | Enable / disable scheduled harvesting |

### Source Agents (individual)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/run-linkedin-agent` | LinkedIn-only harvest |
| `GET` | `/linkedin-results` | List LinkedIn result files |
| `GET` | `/linkedin-results/{run_id}` | Single LinkedIn result |
| `POST` | `/run-naukri-agent` | Naukri-only harvest |
| `GET` | `/naukri-results` | List Naukri result files |
| `GET` | `/naukri-results/{run_id}` | Single Naukri result |
| `POST` | `/run-dice-agent` | Dice-only harvest |
| `GET` | `/dice-results` | List Dice result files |
| `GET` | `/dice-results/{run_id}` | Single Dice result |

### Lead Intelligence

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/run-recruiter-discovery` | ★ Enrich recruiters from harvest results |
| `GET` | `/recruiter-results` | List recruiter discovery runs |
| `GET` | `/recruiter-results/{run_id}` | Full JSON of one recruiter run |
| `POST` | `/run-prospect-intelligence` | Enrich from uploaded prospects.xlsx |
| `GET` | `/prospect-results` | List prospect intelligence runs |
| `GET` | `/prospect-results/{run_id}` | Full JSON of one prospect run |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Server health check |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/openapi.json` | OpenAPI 3.x spec |

---

## Harvest Workflow

```
1. Configure harvest_config.json
   └─ keyword, location, sources, filters, search_window_hours

2. POST /run-harvest-agent  { "config_id": "active" }
   ├─ LinkedIn Agent    ─┐
   ├─ Naukri Agent     ──┼─ run in parallel via asyncio
   └─ Dice Agent       ─┘
         │
         ▼
   Business Filter Service
   └─ classify each job: Direct Client | GCC | Staffing Firm | Ambiguous
         │
         ▼
   Deduplication + Combined JSON
   └─ data/results/combined/{run_id}_combined.json
         │
         ▼
   Excel Report
   └─ data/results/excel/{run_id}_harvest.xlsx

3. Poll GET /run-history until status = "success"
   (Naukri runs take 40–90 min across 76–110 pages)
```

### Expected Execution Times

| Operation | Typical Duration |
|---|---|
| All 3 sources (parallel) | 40–90 min (Naukri is the bottleneck) |
| LinkedIn only | 3–8 min |
| Naukri only | 40–90 min |
| Dice only | 5–15 min |

---

## Lead Intelligence Workflow

```
1. POST /run-recruiter-discovery
   {
     "source_filter": "all",   // all | combined | linkedin | naukri | dice
     "run_ids": [],            // empty = use latest files
     "max_files": 2,           // files per source
     "concurrency": 2          // parallel browser sessions
   }

2. Deduplication
   └─ by linkedin_url → then by name + company

3. 3-Level Contact Discovery (per recruiter)
   ├─ Level 1: Company website /contact, /team, /about → email_status: VERIFIED
   ├─ Level 2: LinkedIn Contact Info modal             → email_status: PUBLIC
   └─ Level 3: Naukri recruiter profile (via DDG)     → email_status: PUBLIC
               No contact found                       → email_status: NOT_FOUND

4. Profile Intelligence Extraction (LinkedIn)
   ├─ position_level    (Recruiter | Manager | Director | VP | CHRO | ...)
   ├─ hiring_domain     (AI/ML | Cloud/DevOps | Java | SAP | ...)
   ├─ company_industry  (LinkedIn taxonomy)
   ├─ company_size      (1,001–5,000 employees)
   ├─ years_in_company  (3 yrs 6 mos)
   └─ overall_experience (9 yrs)

5. Output
   └─ data/results/lead_intelligence/
      ├─ {run_id}_Lead_Intelligence_Report.xlsx  (19 columns)
      └─ {run_id}_recruiter_contacts.json
```

### Lead Intelligence Excel Columns (19)

| # | Column | Description |
|---|---|---|
| 1 | Recruiter Name | Full name |
| 2 | Designation | Job title |
| 3 | Department | Inferred: Talent Acquisition / HR / Leadership |
| 4 | Position Level | Recruiter → Senior Recruiter → Manager → Director → VP → CHRO |
| 5 | Location | LinkedIn location |
| 6 | Current Company | Hiring company |
| 7 | LinkedIn Profile URL | `/in/` URL |
| 8 | Official Email ID | Scraped only — never predicted |
| 9 | Email Status | VERIFIED / PUBLIC / NOT_FOUND |
| 10 | Contact Number | Scraped only — never predicted |
| 11 | Phone Status | VERIFIED / PUBLIC / NOT_FOUND |
| 12 | Hiring Domain | AI/ML, Cloud/DevOps, Java, SAP, ... |
| 13 | Company Industry | LinkedIn taxonomy |
| 14 | Company Size | Employee band (e.g. 1,001–5,000) |
| 15 | Years in Current Company | Tenure from LinkedIn |
| 16 | Overall Experience | Total career experience |
| 17 | Reporting Manager | Direct manager (if public) |
| 18 | Confidence Score | High / Medium / Low |
| 19 | Source | Enrichment sources used |

### Confidence Score Criteria

| Score | Criteria |
|---|---|
| **High** | LinkedIn URL resolved + email found + company domain match |
| **Medium** | LinkedIn URL resolved + company match (no email found) |
| **Low** | LinkedIn URL could not be resolved |

---

## Configuration

Edit `data/config/harvest_config.json` directly or via `PUT /harvest-config`:

```json
{
  "sources": {
    "linkedin": true,
    "naukri": true,
    "dice": true
  },
  "filters": {
    "keyword": "AI Engineer",
    "location": "India",
    "job_type": "Any",
    "work_mode": "Any",
    "search_window_hours": 24,
    "hiring_entity": "Any",
    "gcc_mode": "include_gcc"
  },
  "schedule": {
    "enabled": false,
    "frequency": "daily",
    "run_time": "09:00",
    "timezone": "Asia/Kolkata"
  }
}
```

---

## Data Integrity Policy

- **No email prediction.** Emails are only stored if scraped from a real public source.
- **No phone prediction.** Phone numbers are only stored if scraped from a real public source.
- **No fabrication.** Every contact field defaults to `NOT_FOUND` until a real source is found.
- **Status fields are truthful.** `VERIFIED` = company website. `PUBLIC` = public professional profile. `NOT_FOUND` = genuinely not found.

---

## License

Private — internal enterprise tool. Not for public distribution.
