"""H-1B sponsorship filter.

JD-FIRST LOGIC: The primary signal is the job description itself.
- If the JD explicitly says "no sponsorship" / "cannot sponsor" / etc → block.
- If the JD doesn't mention anything negative about sponsorship → eligible.

H-1B filing data (from USCIS, H1BGrader, etc.) is kept as SUPPLEMENTARY
info only — it enriches jobs with context but never blocks them.

The entire sponsorship filter is a TOGGLE — when disabled, all jobs
pass through with no sponsorship checks at all.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from pipeline.models import CompanyH1BRecord, Job, SponsorshipLikelihood
from pipeline.utils.text import normalize_company_name

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "h1b_cache.db"

# Phrases in a JD that explicitly indicate NO sponsorship.
# We match these case-insensitively.
NO_SPONSORSHIP_PHRASES = [
    # Direct statements
    r"(?:not|cannot|can't|won't|will not|unable to)\s+(?:provide\s+)?sponsor",
    r"no\s+(?:visa\s+)?sponsorship",
    r"without\s+(?:requiring\s+)?(?:visa\s+)?sponsorship",
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"must\s+be\s+(?:legally\s+)?authorized\s+to\s+work\s+(?:in\s+the\s+(?:us|u\.s\.|united\s+states)\s+)?without\s+(?:need\s+for\s+)?(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship",
    r"must\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+(?:visa\s+)?sponsorship",
    # Work authorization without sponsorship
    r"authorized\s+to\s+work\s+(?:in\s+the\s+(?:us|u\.s\.|united\s+states)\s+)?without\s+(?:any\s+)?(?:current\s+or\s+future\s+)?sponsorship",
    r"work\s+authorization\s+(?:is\s+)?required\s+without\s+sponsorship",
    # Visa-specific blocks
    r"(?:not|no|don't|doesn't|do\s+not|does\s+not)\s+(?:offer|support|provide)\s+(?:(?:h-?1b|h1-?b|work)\s+)?visa",
    r"(?:h-?1b|h1-?b|visa)\s+(?:sponsorship\s+)?(?:is\s+)?not\s+(?:available|offered)",
]

# Compile patterns once for performance
_NO_SPONSOR_PATTERNS = [re.compile(p, re.IGNORECASE) for p in NO_SPONSORSHIP_PHRASES]


def check_jd_blocks_sponsorship(description: str) -> bool:
    """Check if a job description explicitly says 'no sponsorship'.

    Returns True if the JD contains language explicitly blocking
    visa sponsorship. Returns False if no such language is found
    (meaning the job is eligible to apply to).
    """
    if not description:
        return False  # No description = no block signal

    for pattern in _NO_SPONSOR_PATTERNS:
        if pattern.search(description):
            return True

    return False


class SponsorshipFilter:
    """Filter and enrich jobs with sponsorship data.

    JD-first approach:
    - Primary signal: Does the JD explicitly say "no sponsorship"?
    - Supplementary data: H-1B filing history (for info, not blocking).
    - Toggle: When enabled=False, no filtering is applied.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        blocklist: list[str] | None = None,
        enabled: bool = True,
    ):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.blocklist = {normalize_company_name(c) for c in (blocklist or [])}
        self.enabled = enabled
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Create the H-1B cache database if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS h1b_filings (
                normalized_name TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                total_filings INTEGER DEFAULT 0,
                recent_filings INTEGER DEFAULT 0,
                swe_filings INTEGER DEFAULT 0,
                approval_rate REAL DEFAULT 0.0,
                last_filing_year INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def lookup_company(self, company_name: str) -> CompanyH1BRecord | None:
        """Look up a company's H-1B filing history from the local cache."""
        normalized = normalize_company_name(company_name)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM h1b_filings WHERE normalized_name = ?",
            (normalized,),
        ).fetchone()
        conn.close()

        if not row:
            return None

        return CompanyH1BRecord(
            company_name=row[1],
            normalized_name=row[0],
            total_filings=row[2],
            recent_filings=row[3],
            swe_filings=row[4],
            approval_rate=row[5],
            last_filing_year=row[6],
        )

    def h1b_likelihood(self, company_name: str) -> SponsorshipLikelihood:
        """Get the H-1B sponsorship likelihood based on filing history.

        This is SUPPLEMENTARY info only — it does not block jobs.
        """
        normalized = normalize_company_name(company_name)

        # Blocklist → LOW but still supplementary
        if normalized in self.blocklist:
            return SponsorshipLikelihood.LOW

        record = self.lookup_company(company_name)
        if not record:
            return SponsorshipLikelihood.UNKNOWN

        if record.recent_filings >= 10 or record.swe_filings >= 5:
            return SponsorshipLikelihood.HIGH
        elif record.recent_filings >= 3 or record.total_filings >= 10:
            return SponsorshipLikelihood.MEDIUM
        else:
            return SponsorshipLikelihood.LOW

    def enrich_jobs(self, jobs: list[Job]) -> list[Job]:
        """Enrich jobs with sponsorship data.

        When enabled:
        - Scans JD for explicit "no sponsorship" language → sets jd_blocks_sponsorship
        - Adds H-1B filing data as supplementary info (never blocks)

        When disabled:
        - All jobs pass through untouched.
        """
        if not self.enabled:
            logger.info("Sponsorship filter disabled — skipping")
            return jobs

        blocked_count = 0
        for job in jobs:
            # PRIMARY: Check the JD text
            job.jd_blocks_sponsorship = check_jd_blocks_sponsorship(job.description)
            if job.jd_blocks_sponsorship:
                blocked_count += 1

            # SUPPLEMENTARY: H-1B filing data (info only)
            job.sponsorship = self.h1b_likelihood(job.company)
            record = self.lookup_company(job.company)
            if record:
                job.h1b_filings_count = record.total_filings

        logger.info(
            f"Sponsorship enriched: {len(jobs)} jobs, "
            f"{blocked_count} blocked by JD language"
        )
        return jobs

    def filter_blocked(self, jobs: list[Job]) -> list[Job]:
        """Remove jobs whose JD explicitly blocks sponsorship.

        Call this AFTER enrich_jobs(). Only filters when enabled.
        """
        if not self.enabled:
            return jobs
        eligible = [j for j in jobs if not j.jd_blocks_sponsorship]
        blocked = len(jobs) - len(eligible)
        if blocked:
            logger.info(f"Filtered out {blocked} jobs with explicit no-sponsorship language")
        return eligible

    def import_h1b_data(self, records: list[dict[str, Any]]) -> int:
        """Bulk import H-1B filing data into the cache.

        Each record should have:
            company_name, total_filings, recent_filings, swe_filings,
            approval_rate, last_filing_year
        """
        conn = sqlite3.connect(self.db_path)
        count = 0
        for rec in records:
            name = rec.get("company_name", "")
            if not name:
                continue
            normalized = normalize_company_name(name)
            conn.execute("""
                INSERT OR REPLACE INTO h1b_filings
                    (normalized_name, company_name, total_filings, recent_filings,
                     swe_filings, approval_rate, last_filing_year)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                normalized,
                name,
                rec.get("total_filings", 0),
                rec.get("recent_filings", 0),
                rec.get("swe_filings", 0),
                rec.get("approval_rate", 0.0),
                rec.get("last_filing_year", 0),
            ))
            count += 1
        conn.commit()
        conn.close()
        logger.info(f"Imported {count} H-1B records")
        return count

    def import_from_json(self, path: Path | str) -> int:
        """Import H-1B data from a JSON file."""
        with open(path) as f:
            records = json.load(f)
        return self.import_h1b_data(records)
