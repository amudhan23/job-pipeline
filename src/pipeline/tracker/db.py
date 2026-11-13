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
        # Each pipeline run is recorded so views can show "what the latest run
        # found" instead of everything ever saved.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                max_age_days INTEGER,
                job_count INTEGER DEFAULT 0
            )
        """)
        # Migrate databases created before runs existed
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "run_id" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN run_id INTEGER")
        if "last_seen" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id)")
        conn.commit()
        conn.close()

    # Columns refreshed from the latest scrape. Application state (status,
    # applied_at, notes, resume_path) is deliberately NOT in this list — a
    # re-run must never reset a job you've marked applied.
    _SCRAPED_FIELDS = (
        "title", "company", "company_slug", "location", "url", "platform",
        "description", "department", "posted_at", "scraped_at", "sponsorship",
        "h1b_filings_count", "fit_score", "fit_breakdown", "run_id", "last_seen",
    )

    def _row(self, job: Job, run_id: int | None, now: str) -> tuple:
        return (
            job.id, job.title, job.company, job.company_slug, job.location,
            job.url, job.platform.value, job.description[:5000], job.department,
            job.posted_at.isoformat() if job.posted_at else None,
            job.scraped_at.isoformat(),
            job.sponsorship.value, job.h1b_filings_count,
            job.fit_score, json.dumps(job.fit_breakdown),
            run_id, now,
            job.resume_path, job.status.value,
            job.applied_at.isoformat() if job.applied_at else None,
            job.notes,
        )

    def upsert_jobs(self, jobs: list[Job], run_id: int | None = None) -> int:
        """Insert new jobs; refresh scraped fields of existing ones while
        keeping their application status. One transaction for the batch."""
        now = datetime.now().isoformat(timespec="seconds")
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._SCRAPED_FIELDS)
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.executemany(f"""
                INSERT INTO jobs
                    (id, title, company, company_slug, location, url, platform,
                     description, department, posted_at, scraped_at, sponsorship,
                     h1b_filings_count, fit_score, fit_breakdown, run_id, last_seen,
                     resume_path, status, applied_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET {updates}
            """, [self._row(j, run_id, now) for j in jobs])
        conn.close()
        return len(jobs)

    def upsert_job(self, job: Job, run_id: int | None = None) -> None:
        self.upsert_jobs([job], run_id)

    # ── Runs ────────────────────────────────────────────────────────────

    def start_run(self, max_age_days: int | None) -> int:
        conn = sqlite3.connect(self.db_path)
        with conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, max_age_days) VALUES (?, ?)",
                (datetime.now().isoformat(timespec="seconds"), max_age_days),
            )
        conn.close()
        return cur.lastrowid

    def finish_run(self, run_id: int, job_count: int) -> None:
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("UPDATE runs SET job_count = ? WHERE id = ?", (job_count, run_id))
        conn.close()

    def latest_run(self) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

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

    def _scope(self, run_id: int | None) -> tuple[str, list[Any]]:
        """WHERE clause for a run scope. run_id=None means all saved jobs."""
        return ("WHERE run_id = ?", [run_id]) if run_id is not None else ("WHERE 1=1", [])

    def get_jobs(self, run_id: int | None = None) -> list[dict[str, Any]]:
        """All jobs from one run (or every saved job), best score first."""
        where, params = self._scope(run_id)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY fit_score DESC", params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_top_jobs(
        self, limit: int = 20, status: str | None = None, run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Top-scored jobs, optionally limited to a status and/or one run."""
        where, params = self._scope(run_id)
        if status:
            where += " AND status = ?"
            params.append(status)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY fit_score DESC LIMIT ?", [*params, limit]
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_stats(self, run_id: int | None = None) -> dict[str, int]:
        """Job counts by application status, for one run or everything."""
        where, params = self._scope(run_id)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM jobs {where} GROUP BY status", params
        ).fetchall()
        conn.close()
        stats = dict(rows)
        stats["total"] = sum(stats.values())
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
