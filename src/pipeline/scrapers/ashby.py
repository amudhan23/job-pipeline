"""Ashby ATS scraper.

Ashby exposes a public API at:
    POST https://jobs.ashbyhq.com/api/non-user-graphql
    with operation "apiJobBoardWithTeams" for listing

Also has JSON endpoint:
    https://api.ashbyhq.com/posting-api/job-board/{company_slug}
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyScraper(BaseScraper):
    """Scrape jobs from Ashby public API."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        """Scrape all matching jobs from an Ashby job board."""
        jobs: list[Job] = []
        url = f"{ASHBY_API}/{company_slug}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("jobs", []):
                title = item.get("title", "")
                location = item.get("location", "")
                # Ashby may have locationName or location as a string
                if isinstance(location, dict):
                    location = location.get("name", "")

                desc = item.get("descriptionPlain", "") or item.get("description", "")

                if not self.passes_filters(title, location, desc):
                    continue

                job_id = item.get("id", "")
                job = Job(
                    id=f"ab-{company_slug}-{job_id}",
                    title=title,
                    company=data.get("organization", {}).get("name", company_slug),
                    company_slug=company_slug,
                    location=location,
                    url=item.get("jobUrl", f"https://jobs.ashbyhq.com/{company_slug}/{job_id}"),
                    platform=ATSPlatform.ASHBY,
                    description=desc,
                    department=item.get("department", ""),
                    posted_at=_parse_date(item.get("publishedAt")),
                )
                jobs.append(job)

        return jobs


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
