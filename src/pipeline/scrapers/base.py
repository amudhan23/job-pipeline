"""Base scraper interface."""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from pipeline.models import Job

logger = logging.getLogger(__name__)

_US_WORDS = ("us", "usa", "u.s.", "united states")


def _has_word(text: str, word: str) -> bool:
    """Whole-word, case-insensitive match (handles "sr." and "co-op")."""
    return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text) is not None


class BaseScraper(ABC):
    """Base class for ATS scrapers."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.filters = config.get("filters", {})

    @abstractmethod
    async def scrape_company(self, company_slug: str) -> list[Job]:
        """Scrape all matching jobs from a single company."""
        ...

    async def scrape_many(
        self, company_slugs: list[str], concurrency: int = 8,
    ) -> tuple[list[Job], dict[str, int | None]]:
        """Scrape many boards concurrently.

        Returns (jobs, status) where status maps each slug to the number of
        matching jobs, or None if that board failed (e.g. 404 — the company
        left this ATS). The registry uses status to retire dead boards.
        """
        sem = asyncio.Semaphore(concurrency)
        status: dict[str, int | None] = {}

        async def one(slug: str) -> list[Job]:
            async with sem:
                try:
                    jobs = await self.scrape_company(slug)
                    status[slug] = len(jobs)
                    logger.debug(f"Scraped {len(jobs)} jobs from {slug}")
                    return jobs
                except Exception as e:
                    status[slug] = None
                    logger.debug(f"Failed to scrape {slug}: {e}")
                    return []

        results = await asyncio.gather(*(one(s) for s in company_slugs))
        return [job for batch in results for job in batch], status

    async def scrape_all(self, company_slugs: list[str]) -> list[Job]:
        """Scrape all matching jobs from multiple companies."""
        jobs, _ = await self.scrape_many(company_slugs)
        return jobs

    def passes_title(self, title: str) -> bool:
        """Role keyword present and no exclude keyword (whole words)."""
        title_lower = title.lower()
        role_keywords = self.filters.get("role_keywords", [])
        if role_keywords and not any(kw.lower() in title_lower for kw in role_keywords):
            return False
        # Whole words, so "sr" catches "Sr Software Engineer" but not "Israel"
        exclude = self.filters.get("exclude_keywords", [])
        return not any(_has_word(title_lower, exc) for exc in exclude)

    def passes_filters(self, title: str, location: str, description: str = "") -> bool:
        """Check if a job passes the configured filters."""
        if not self.passes_title(title):
            return False

        target_locations = self.filters.get("locations", [])
        if not target_locations:
            return True

        # "US-NY-New-York" (Workday style) should match "New York"
        loc_lower = re.sub(r"[-_]+", " ", location.lower())
        if not loc_lower.strip():
            # No location given — fall back to the JD saying it's remote
            return "remote" in description.lower()[:500]

        # "Remote" is a target but says nothing about the country, so it
        # doesn't count as a US signal ("Remote Canada" must still be rejected)
        us_signal = any(
            t.lower() in loc_lower for t in target_locations if t.lower() != "remote"
        ) or any(_has_word(loc_lower, w) for w in _US_WORDS)
        # Foreign location with no US mention ("Remote Canada", "Bangalore, India")
        if not us_signal and any(
            _has_word(loc_lower, x) for x in self.filters.get("exclude_locations", [])
        ):
            return False

        return any(t.lower() in loc_lower for t in target_locations) or "remote" in loc_lower
