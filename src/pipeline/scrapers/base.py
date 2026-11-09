"""Base scraper interface."""

from __future__ import annotations

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

    async def scrape_all(self, company_slugs: list[str]) -> list[Job]:
        """Scrape all matching jobs from multiple companies."""
        all_jobs: list[Job] = []
        for slug in company_slugs:
            try:
                jobs = await self.scrape_company(slug)
                all_jobs.extend(jobs)
                logger.info(f"Scraped {len(jobs)} jobs from {slug}")
            except Exception as e:
                logger.error(f"Failed to scrape {slug}: {e}")
        return all_jobs

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
