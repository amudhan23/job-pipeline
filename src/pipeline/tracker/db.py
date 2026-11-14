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
        conn = self._connect()
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
        if "ai_fit" not in cols:
            # AI fit check result (JSON); kept across re-scrapes like status
            conn.execute("ALTER TABLE jobs ADD COLUMN ai_fit TEXT DEFAULT ''")
        # Real-time crawling: when we first saw a posting, when it vanished
        # from its board, and whether it was already up when we started
        # watching that board (baseline — not "new").
        for col, ddl in (("first_seen_at", "TEXT"), ("closed_at", "TEXT"),
                         ("baseline", "INTEGER DEFAULT 0"), ("source", "TEXT DEFAULT 'board'")):
            if col not in cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        if "first_seen_at" not in cols:
            conn.execute("UPDATE jobs SET first_seen_at = COALESCE(scraped_at, last_seen), baseline = 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(platform, company_slug)")
        # What happened, when: new / closed / reopened postings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                at TEXT NOT NULL,
                detail TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_at ON job_events(at DESC)")
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Connection that tolerates the crawler writing while the dashboard reads."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

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
        conn = self._connect()
        with conn:
            conn.executemany(f"""
                INSERT INTO jobs
                    (id, title, company, company_slug, location, url, platform,
                     description, department, posted_at, scraped_at, sponsorship,
                     h1b_filings_count, fit_score, fit_breakdown, run_id, last_seen,
                     resume_path, status, applied_at, notes, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET {updates}
            """, [(*self._row(j, run_id, now), now) for j in jobs])
        conn.close()
        return len(jobs)

    def upsert_job(self, job: Job, run_id: int | None = None) -> None:
        self.upsert_jobs([job], run_id)

    # ── Runs ────────────────────────────────────────────────────────────

    def start_run(self, max_age_days: int | None) -> int:
        conn = self._connect()
        with conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, max_age_days) VALUES (?, ?)",
                (datetime.now().isoformat(timespec="seconds"), max_age_days),
            )
        conn.close()
        return cur.lastrowid

    def finish_run(self, run_id: int, job_count: int) -> None:
        conn = self._connect()
        with conn:
            conn.execute("UPDATE runs SET job_count = ? WHERE id = ?", (job_count, run_id))
        conn.close()

    def latest_run(self) -> dict[str, Any] | None:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

    def update_status(self, job_id: str, status: ApplicationStatus, notes: str = "") -> None:
        """Update job application status."""
        conn = self._connect()
        now = datetime.now().isoformat() if status == ApplicationStatus.APPLIED else None
        conn.execute(
            "UPDATE jobs SET status = ?, applied_at = COALESCE(?, applied_at), notes = ? WHERE id = ?",
            (status.value, now, notes, job_id),
        )
        conn.commit()
        conn.close()

    def set_ai_fit(self, job_id: str, result: dict[str, Any]) -> None:
        import json as _json
        conn = self._connect()
        conn.execute("UPDATE jobs SET ai_fit = ? WHERE id = ?", (_json.dumps(result), job_id))
        conn.commit()
        conn.close()

    # ── Real-time crawling ──────────────────────────────────────────────

    def known_ids(self, platform: str, slug: str) -> dict[str, str | None]:
        """Job ids we hold for one board → their closed_at (None = open)."""
        conn = self._connect()
        rows = conn.execute("SELECT id, closed_at FROM jobs WHERE platform = ? AND company_slug = ?",
                            (platform, slug)).fetchall()
        conn.close()
        return dict(rows)

    def observe_board(self, platform: str, slug: str, jobs: list[Job], baseline: bool = False,
                      close_missing: bool = True) -> tuple[list[str], list[str], list[str]]:
        """Record one successful read of a board: every relevant posting on it
        right now. New ids are inserted (first_seen_at = now; baseline=1 on
        the first read of a board, so pre-existing postings aren't "new"),
        known ones refreshed (status kept), ones that vanished are closed,
        ones that came back reopened. Returns (new, closed, reopened) ids."""
        now = datetime.now().isoformat(timespec="seconds")
        known = self.known_ids(platform, slug)
        seen = {j.id for j in jobs}
        new = [j.id for j in jobs if j.id not in known]
        reopened = [j.id for j in jobs if j.id in known and known[j.id]]
        closed = [i for i, c in known.items() if c is None and i not in seen] if close_missing else []
        # Known postings: only what a listing can change. Description, score
        # and application state were set when we first saw the job.
        updates = "title = excluded.title, location = excluded.location, url = excluded.url, " \
                  "last_seen = excluded.last_seen"
        conn = self._connect()
        with conn:
            conn.executemany(f"""
                INSERT INTO jobs
                    (id, title, company, company_slug, location, url, platform,
                     description, department, posted_at, scraped_at, sponsorship,
                     h1b_filings_count, fit_score, fit_breakdown, run_id, last_seen,
                     resume_path, status, applied_at, notes, first_seen_at, baseline, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'board')
                ON CONFLICT(id) DO UPDATE SET {updates}, closed_at = NULL
            """, [(*self._row(j, None, now), now, 1 if baseline else 0) for j in jobs])
            if closed:
                conn.executemany("UPDATE jobs SET closed_at = ? WHERE id = ?", [(now, i) for i in closed])
            conn.executemany("INSERT INTO job_events (job_id, kind, at) VALUES (?, ?, ?)",
                             [(i, "new", now) for i in new if not baseline]
                             + [(i, "closed", now) for i in closed]
                             + [(i, "reopened", now) for i in reopened])
        conn.close()
        return new, closed, reopened

    def insert_feed_jobs(self, jobs: list[Job], baseline: bool = False) -> list[str]:
        """Jobs from a feed whose board we can't crawl (Workday, iCIMS…):
        insert new ones (as new events), refresh known ones. Returns new ids."""
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._connect()
        known = {r[0] for r in conn.execute(
            f"SELECT id FROM jobs WHERE id IN ({','.join('?' * len(jobs))})", [j.id for j in jobs])} \
            if jobs else set()
        new = [j.id for j in jobs if j.id not in known]
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._SCRAPED_FIELDS if c != "run_id")
        with conn:
            conn.executemany(f"""
                INSERT INTO jobs
                    (id, title, company, company_slug, location, url, platform,
                     description, department, posted_at, scraped_at, sponsorship,
                     h1b_filings_count, fit_score, fit_breakdown, run_id, last_seen,
                     resume_path, status, applied_at, notes, first_seen_at, baseline, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'feed')
                ON CONFLICT(id) DO UPDATE SET {updates}, closed_at = NULL
            """, [(*self._row(j, None, now), now, 1 if baseline else 0) for j in jobs])
            if not baseline:
                conn.executemany("INSERT INTO job_events (job_id, kind, at) VALUES (?, 'new', ?)",
                                 [(i, now) for i in new])
        conn.close()
        return new

    def open_feed_ids(self) -> set[str]:
        conn = self._connect()
        ids = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE source = 'feed' AND closed_at IS NULL")}
        conn.close()
        return ids

    def close_jobs(self, ids: list[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._connect()
        with conn:
            conn.executemany("UPDATE jobs SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                             [(now, i) for i in ids])
            conn.executemany("INSERT INTO job_events (job_id, kind, at) VALUES (?, 'closed', ?)",
                             [(i, now) for i in ids])
        conn.close()

    def open_jobs(self, max_age_hours: float | None = None, seen_within_hours: float = 72,
                  limit: int | None = None) -> list[dict[str, Any]]:
        """Open postings (not closed, seen recently), freshest first.
        max_age_hours keeps only jobs that appeared (or were posted) that recently."""
        from datetime import timedelta
        now = datetime.now()
        seen_cut = (now - timedelta(hours=seen_within_hours)).isoformat(timespec="seconds")
        where = ["closed_at IS NULL", "COALESCE(last_seen, scraped_at) >= ?"]
        params: list[Any] = [seen_cut]
        if max_age_hours is not None:
            cut = (now - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
            where.append("((COALESCE(baseline, 0) = 0 AND first_seen_at >= ?) OR posted_at >= ?)")
            params += [cut, cut]
        sql = (f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY "
               "CASE WHEN COALESCE(baseline, 0) = 0 THEN first_seen_at ELSE COALESCE(posted_at, first_seen_at) END DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def events(self, since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        sql = ("SELECT e.*, j.title, j.company, j.fit_score FROM job_events e "
               "LEFT JOIN jobs j ON j.id = e.job_id")
        params: list[Any] = []
        if since:
            sql += " WHERE e.at >= ?"
            params.append(since)
        sql += " ORDER BY e.id DESC LIMIT ?"
        rows = conn.execute(sql, [*params, limit]).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(f"SELECT * FROM jobs WHERE status IN ({marks}) "
                            "ORDER BY COALESCE(applied_at, last_seen) DESC", statuses).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get a single job by ID."""
        conn = self._connect()
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
        conn = self._connect()
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
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY fit_score DESC LIMIT ?", [*params, limit]
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_stats(self, run_id: int | None = None) -> dict[str, int]:
        """Job counts by application status, for one run or everything."""
        where, params = self._scope(run_id)
        conn = self._connect()
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
        conn = self._connect()
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
