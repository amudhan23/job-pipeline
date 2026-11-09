"""Core data models for the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ATSPlatform(str, Enum):
    GREENHOUSE = "greenhouse"
    ASHBY = "ashby"
    LEVER = "lever"
    LINKEDIN = "linkedin"
    WORKDAY = "workday"
    OTHER = "other"


class SponsorshipLikelihood(str, Enum):
    HIGH = "high"        # 10+ recent H-1B filings
    MEDIUM = "medium"    # Some filings
    LOW = "low"          # No filing history
    UNKNOWN = "unknown"  # Not enough data


class ApplicationStatus(str, Enum):
    SCRAPED = "scraped"
    SCORED = "scored"
    RESUME_GENERATED = "resume_generated"
    APPLIED = "applied"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"
    GHOSTED = "ghosted"
    SKIPPED = "skipped"


@dataclass
class Job:
    """A scraped job posting."""
    id: str
    title: str
    company: str
    company_slug: str  # ATS company identifier
    location: str
    url: str
    platform: ATSPlatform
    description: str = ""
    department: str = ""
    posted_at: datetime | None = None
    scraped_at: datetime = field(default_factory=datetime.now)

    # Enriched fields (filled by later stages)
    sponsorship: SponsorshipLikelihood = SponsorshipLikelihood.UNKNOWN
    h1b_filings_count: int = 0
    jd_blocks_sponsorship: bool = False  # True if JD explicitly says "no sponsorship"
    fit_score: float = 0.0
    fit_breakdown: dict[str, float] = field(default_factory=dict)
    resume_path: str | None = None
    status: ApplicationStatus = ApplicationStatus.SCRAPED
    applied_at: datetime | None = None
    notes: str = ""


@dataclass
class CompanyH1BRecord:
    """H-1B filing data for a company."""
    company_name: str
    normalized_name: str
    total_filings: int = 0
    recent_filings: int = 0  # Last 2 years
    swe_filings: int = 0     # Software engineering roles
    approval_rate: float = 0.0
    last_filing_year: int = 0


@dataclass
class ResumeConfig:
    """Configuration for tailoring a resume to a specific job."""
    job_id: str
    ibm_bullet_order: list[str] = field(default_factory=lambda: ["own", "mig", "dbg", "ci", "db"])
    sahara_bullet_order: list[str] = field(default_factory=lambda: ["ship", "int", "tx"])
    projects: list[str] = field(default_factory=lambda: ["req-guard", "AccessMint", "Two-Pass Linker", "Commentator"])
    language_order: list[str] = field(default_factory=lambda: ["Java", "Rust", "Python", "TypeScript", "JavaScript", "C++", "C", "SQL", "Bash"])
    include_location: bool = True
    output_path: str = ""


@dataclass
class Profile:
    """Applicant profile for form filling."""
    full_name: str = "Amudhan Muthaiah"
    email: str = "am15241@nyu.edu"
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    university: str = "New York University"
    degree: str = "Master of Science in Computer Science"
    graduation: str = "May 2026"
    work_authorized: bool = True
    needs_sponsorship: bool = True
    years_experience: int = 3
    willing_to_relocate: bool = True
    locations: list[str] = field(default_factory=lambda: ["New York, NY", "San Francisco, CA", "Remote"])
