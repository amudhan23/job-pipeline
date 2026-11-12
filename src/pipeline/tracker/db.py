"""SQLite-backed application tracker."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.models import ApplicationStatus, Job

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "tracker.db"


class Tracker:
    """Track job applications in SQLite."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._ensure_db()

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                company_slug TEXT,
                location TEXT,
                url TEXT,
                platform TEXT,
                description TEXT,
                department TEXT,
                posted_at TEXT,
                scraped_at TEXT,
                sponsorship TEXT DEFAULT 'unknown',
                h1b_filings_count INTEGER DEFAULT 0,
                fit_score REAL DEFAULT 0,
                fit_breakdown TEXT DEFAULT '{}',
                resume_path TEXT,
                status TEXT DEFAULT 'scraped',
                applied_at TEXT,
                notes TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(fit_score DESC)
        """)
        conn.commit()
        conn.close()

    def upsert_job(self, job: Job) -> None:
        """Insert or update a job record."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO jobs
                (id, title, company, company_slug, location, url, platform,
                 description, department, posted_at, scraped_at, sponsorship,
                 h1b_filings_count, fit_score, fit_breakdown, resume_path,
                 status, applied_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job.id, job.title, job.company, job.company_slug, job.location,
            job.url, job.platform.value, job.description[:5000], job.department,
            job.posted_at.isoformat() if job.posted_at else None,
            job.scraped_at.isoformat(),
            job.sponsorship.value, job.h1b_filings_count,
            job.fit_score, json.dumps(job.fit_breakdown),
            job.resume_path, job.status.value,
            job.applied_at.isoformat() if job.applied_at else None,
            job.notes,
        ))
        conn.commit()
        conn.close()

    def upsert_jobs(self, jobs: list[Job]) -> int:
        """Bulk upsert jobs. Returns count."""
        for job in jobs:
            self.upsert_job(job)
        return len(jobs)

    def update_status(self, job_id: str, status: ApplicationStatus, notes: str = "") -> None:
        """Update job application status."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat() if status == ApplicationStatus.APPLIED else None
        conn.execute(
            "UPDATE jobs SET status = ?, applied_at = COALESCE(?, applied_at), notes = ? WHERE id = ?",
            (status.value, now, notes, job_id),
        )
        conn.commit()
        conn.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get a single job by ID."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_top_jobs(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        """Get top-scored jobs, optionally filtered by status."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY fit_score DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, int]:
        """Get application pipeline stats."""
        conn = sqlite3.connect(self.db_path)
        stats: dict[str, int] = {}
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        ).fetchall()
        for status, count in rows:
            stats[status] = count
        stats["total"] = sum(stats.values())
        conn.close()
        return stats

    def export_csv(self, path: Path | str) -> None:
        """Export tracker to CSV."""
        import csv
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, company, location, url, platform, sponsorship, "
            "fit_score, status, applied_at, notes FROM jobs ORDER BY fit_score DESC"
        ).fetchall()
        conn.close()

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        logger.info(f"Exported {len(rows)} jobs to {path}")
