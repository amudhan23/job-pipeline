"""H-1B sponsorship filter.

Cross-references companies against public H-1B filing databases:
- USCIS Employer Data Hub (https://www.uscis.gov/h-1b-data-hub)
- H1BGrader (https://h1bgrader.com)
- MyVisaJobs (https://www.myvisajobs.com)
- h1bdata.info (https://h1bdata.info)

Since these sites don't have stable public APIs, we use a local cache
that's populated by periodic scraping or manual data imports.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pipeline.models import CompanyH1BRecord, Job, SponsorshipLikelihood
from pipeline.utils.text import normalize_company_name

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "h1b_cache.db"


class SponsorshipFilter:
    """Filter and score jobs by H-1B sponsorship likelihood."""

    def __init__(self, db_path: Path | str | None = None, blocklist: list[str] | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.blocklist = {normalize_company_name(c) for c in (blocklist or [])}
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

    def score_company(self, company_name: str) -> SponsorshipLikelihood:
        """Score a company's sponsorship likelihood."""
        normalized = normalize_company_name(company_name)

        # Check blocklist first
        if normalized in self.blocklist:
            return SponsorshipLikelihood.LOW

        record = self.lookup_company(company_name)
        if not record:
            return SponsorshipLikelihood.UNKNOWN

        # Scoring logic
        if record.recent_filings >= 10 or record.swe_filings >= 5:
            return SponsorshipLikelihood.HIGH
        elif record.recent_filings >= 3 or record.total_filings >= 10:
            return SponsorshipLikelihood.MEDIUM
        else:
            return SponsorshipLikelihood.LOW

    def enrich_jobs(self, jobs: list[Job]) -> list[Job]:
        """Add sponsorship data to a list of jobs."""
        for job in jobs:
            job.sponsorship = self.score_company(job.company)
            record = self.lookup_company(job.company)
            if record:
                job.h1b_filings_count = record.total_filings
        return jobs

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
