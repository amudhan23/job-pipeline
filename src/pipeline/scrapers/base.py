"""Base scraper interface."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from pipeline.models import Job

logger = logging.getLogger(__name__)


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

    def passes_filters(self, title: str, location: str, description: str = "") -> bool:
        """Check if a job passes the configured filters."""
        title_lower = title.lower()

        # Must match at least one role keyword
        role_keywords = self.filters.get("role_keywords", [])
        if role_keywords and not any(kw.lower() in title_lower for kw in role_keywords):
            return False

        # Must not match any exclude keyword
        exclude = self.filters.get("exclude_keywords", [])
        if any(exc.lower() in title_lower for exc in exclude):
            return False

        # Location check (if locations configured)
        target_locations = self.filters.get("locations", [])
        if target_locations:
            loc_lower = location.lower()
            if not any(loc.lower() in loc_lower for loc in target_locations):
                # Also check description for remote
                if "remote" not in loc_lower and "remote" not in description.lower()[:500]:
                    return False

        return True
