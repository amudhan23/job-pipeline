"""Board registry — every company job board the web has ever surfaced.

The pipeline doesn't start from a company list. Each run discovers boards
from the web (search results, job feeds) and records them here. On later
runs every known board is re-scraped, so a company found once keeps being
checked even if it stops ranking in search.

Boards that fail repeatedly (404 = company left that ATS or closed its
board) are retired automatically after MAX_FAILURES consecutive failures.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "registry.db"
MAX_FAILURES = 3


@dataclass
class Board:
    platform: str
    slug: str
    source: str
    first_seen: str
    last_seen: str
    last_job_count: int
    fail_count: int
    name: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or pretty_slug(self.slug)


def pretty_slug(slug: str) -> str:
    """Readable fallback name for a board slug: "humble-robotics" -> "Humble Robotics"."""
    return slug.replace("-", " ").replace("_", " ").title()


_COLS = "platform, slug, source, first_seen, last_seen, last_job_count, fail_count, name"

_FRONTIER_COLS = (
    ("next_due_at", "TEXT"),            # ISO time the board should next be read (NULL = now)
    ("interval_s", "REAL"),             # current revisit interval
    ("rate", "REAL"),                   # estimated relevant new postings per day
    ("etag", "TEXT"),
    ("last_modified", "TEXT"),
    ("content_hash", "TEXT"),           # of the last listing body — skip parsing when unchanged
    ("last_fetch_at", "TEXT"),
    ("last_change_at", "TEXT"),         # last time a relevant posting appeared
    ("last_status", "INTEGER"),
    ("fetches", "INTEGER DEFAULT 0"),
    ("not_modified", "INTEGER DEFAULT 0"),
    ("new_total", "INTEGER DEFAULT 0"),
    ("bootstrapped", "INTEGER DEFAULT 0"),   # first full read done (its postings are the baseline)
    ("error_streak", "INTEGER DEFAULT 0"),
)


class BoardRegistry:
    """SQLite-backed set of (platform, slug) job boards."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS boards (
                    platform TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_job_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    PRIMARY KEY (platform, slug)
                )
            """)
            # Company display name ("Kalshi" for ashby/kalshi), learned from
            # feeds. Added after the first release, so migrate old databases.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(boards)")}
            if "name" not in cols:
                conn.execute("ALTER TABLE boards ADD COLUMN name TEXT")
            # Crawl frontier: when each board is due, how often it changes,
            # and the validators for conditional requests
            for col, ddl in _FRONTIER_COLS:
                if col not in cols:
                    conn.execute(f"ALTER TABLE boards ADD COLUMN {col} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_boards_due ON boards(next_due_at)")

    def _conn(self) -> sqlite3.Connection:
        # The crawler writes while the dashboard reads: WAL + wait instead of "database is locked"
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def add(self, platform: str, slug: str, source: str) -> bool:
        """Record a board sighting. Returns True if the board is new."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE boards SET last_seen = ? WHERE platform = ? AND slug = ?",
                (now, platform, slug),
            )
            if cur.rowcount:
                return False
            conn.execute(
                "INSERT INTO boards (platform, slug, source, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (platform, slug, source, now, now),
            )
            return True

    def add_many(self, boards: set[tuple[str, str]], source: str) -> int:
        """Record many sightings. Returns how many were new."""
        return sum(self.add(p, s, source) for p, s in boards)

    def active(self, platform: str | None = None) -> list[Board]:
        """Boards that haven't been retired for repeated failures."""
        query = f"SELECT {_COLS} FROM boards WHERE fail_count < ?"
        params: list = [MAX_FAILURES]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY platform, slug"
        with self._conn() as conn:
            return [Board(*row) for row in conn.execute(query, params)]

    def slugs(self, platform: str) -> list[str]:
        return [b.slug for b in self.active(platform)]

    def record_result(self, platform: str, slug: str, job_count: int | None) -> None:
        """Record a scrape outcome. job_count=None means the scrape failed."""
        with self._conn() as conn:
            if job_count is None:
                conn.execute(
                    "UPDATE boards SET fail_count = fail_count + 1 "
                    "WHERE platform = ? AND slug = ?",
                    (platform, slug),
                )
            else:
                conn.execute(
                    "UPDATE boards SET fail_count = 0, last_job_count = ? "
                    "WHERE platform = ? AND slug = ?",
                    (job_count, platform, slug),
                )

    def counts(self) -> dict[str, int]:
        """Active board count per platform."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT platform, COUNT(*) FROM boards WHERE fail_count < ? GROUP BY platform",
                (MAX_FAILURES,),
            ).fetchall()
        return dict(rows)

    def all(self) -> list[Board]:
        with self._conn() as conn:
            return [Board(*row) for row in conn.execute(
                f"SELECT {_COLS} FROM boards ORDER BY first_seen DESC"
            )]

    def set_names(self, names: dict[tuple[str, str], str]) -> None:
        """Remember company display names (from feeds) for boards."""
        with self._conn() as conn:
            conn.executemany(
                "UPDATE boards SET name = ? WHERE platform = ? AND slug = ?",
                [(n, p, s) for (p, s), n in names.items() if n],
            )

    # ── Frontier ────────────────────────────────────────────────────────

    def due(self, now: str, limit: int = 50) -> list[dict]:
        """Active boards whose next read is due, most overdue first
        (never-read boards first of all)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM boards WHERE fail_count < ? AND (next_due_at IS NULL OR next_due_at <= ?) "
                "ORDER BY next_due_at IS NOT NULL, next_due_at LIMIT ?",
                (MAX_FAILURES, now, limit)).fetchall()
        return [dict(r) for r in rows]

    def next_due(self) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT MIN(COALESCE(next_due_at, '')) FROM boards WHERE fail_count < ?",
                               (MAX_FAILURES,)).fetchone()
        return row[0] if row else None

    def schedule(self, platform: str, slug: str, **fields) -> None:
        """Update frontier fields for one board (only known columns)."""
        allowed = {c for c, _ in _FRONTIER_COLS} | {"fail_count", "last_job_count", "name"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self._conn() as conn:
            conn.execute(f"UPDATE boards SET {assignments} WHERE platform = ? AND slug = ?",
                         (*sets.values(), platform, slug))

    def bump(self, platform: str, slug: str, *, fetched: bool, not_modified: bool, new: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE boards SET fetches = fetches + ?, not_modified = not_modified + ?, "
                         "new_total = new_total + ? WHERE platform = ? AND slug = ?",
                         (int(fetched), int(not_modified), new, platform, slug))

    def make_due_now(self, boards: set[tuple[str, str]]) -> None:
        """A feed just showed a posting on these boards: read them next."""
        with self._conn() as conn:
            conn.executemany("UPDATE boards SET next_due_at = NULL WHERE platform = ? AND slug = ?",
                             list(boards))

    def frontier_stats(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(fetches), SUM(not_modified), SUM(new_total), "
                "AVG(interval_s), SUM(bootstrapped) FROM boards WHERE fail_count < ?",
                (MAX_FAILURES,)).fetchone()
        total, fetches, nm, new, avg_iv, boot = row
        return {"boards": total or 0, "fetches": fetches or 0, "not_modified": nm or 0,
                "new_total": new or 0, "avg_interval_min": round((avg_iv or 0) / 60, 1),
                "bootstrapped": boot or 0}

    def names(self) -> dict[tuple[str, str], str]:
        with self._conn() as conn:
            return {(p, s): n for p, s, n in conn.execute(
                "SELECT platform, slug, name FROM boards WHERE name IS NOT NULL"
            )}
