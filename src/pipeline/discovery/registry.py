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

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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

    def names(self) -> dict[tuple[str, str], str]:
        with self._conn() as conn:
            return {(p, s): n for p, s, n in conn.execute(
                "SELECT platform, slug, name FROM boards WHERE name IS NOT NULL"
            )}
