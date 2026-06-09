"""
Pydantic response models for the Harvest Agent API.
"""
from __future__ import annotations

from pydantic import BaseModel


class HarvestJob(BaseModel):
    """One scraped job listing."""
    title:     str
    company:   str
    location:  str
    posted:    str
    job_url:   str
    work_mode: str
    source:    str = "LinkedIn"


class HarvestFiltersSnapshot(BaseModel):
    """The filter values that were active during a run."""
    keyword:             str
    location:            str
    job_type:            str
    work_mode:           str
    search_window_hours: int
    max_jobs:            int


class HarvestRunResponse(BaseModel):
    """Response body for POST /run-harvest."""
    status:      str
    run_id:      str
    executed_at: str
    total_jobs:  int
    saved_to:    str
    source:      str
    filters:     HarvestFiltersSnapshot
    jobs:        list[HarvestJob]


class ResultFileSummary(BaseModel):
    """One row in GET /harvest-results listing."""
    run_id:      str
    executed_at: str
    status:      str
    total_found: int
    keyword:     str
    location:    str
    file_path:   str


class ResultsListResponse(BaseModel):
    """Response body for GET /harvest-results."""
    total_runs: int
    results:    list[ResultFileSummary]


class ScheduleStatusResponse(BaseModel):
    """Response body for GET /harvest-schedule/status."""
    enabled:   bool
    frequency: str
    run_time:  str
    timezone:  str
    next_run:  str | None = None


class NaukriJob(BaseModel):
    """One scraped Naukri job listing — richer than HarvestJob."""
    job_title:       str
    company:         str
    location:        str
    salary:          str = "Not Disclosed"
    experience:      str = "Not Specified"
    posted_date:     str
    job_url:         str
    job_description: str = ""
    skills:          list[str] = []
    source:          str = "Naukri"


class NaukriRunResponse(BaseModel):
    """Response body for POST /run-naukri-agent."""
    run_id:      str
    status:      str
    source:      str = "Naukri"
    total_found: int
    executed_at: str
    saved_to:    str
    jobs:        list[NaukriJob]


class LinkedInJob(BaseModel):
    """One scraped LinkedIn job listing."""
    job_title:       str
    company:         str
    location:        str
    salary:          str       = "Not Disclosed"
    experience:      str       = "Not Specified"
    posted_date:     str
    job_url:         str
    job_description: str       = ""
    skills:          list[str] = []
    work_mode:       str       = "not_specified"
    company_url:     str       = ""
    employment_type: str       = ""
    source:          str       = "LinkedIn"


class LinkedInRunResponse(BaseModel):
    """Response body for POST /run-linkedin-agent."""
    run_id:      str
    status:      str
    source:      str = "LinkedIn"
    total_found: int
    executed_at: str
    saved_to:    str
    jobs:        list[LinkedInJob]


class DiceJob(BaseModel):
    """One scraped Dice.com job listing."""
    job_title:       str
    company:         str
    location:        str
    salary:          str       = "Not Disclosed"
    experience:      str       = "Not Specified"
    posted_date:     str
    job_url:         str
    job_description: str       = ""
    skills:          list[str] = []
    work_mode:       str       = "not_specified"
    employment_type: str       = ""
    source:          str       = "Dice"


class DiceRunResponse(BaseModel):
    """Response body for POST /run-dice-agent."""
    run_id:      str
    status:      str
    source:      str = "Dice"
    total_found: int
    executed_at: str
    saved_to:    str
    jobs:        list[DiceJob]
