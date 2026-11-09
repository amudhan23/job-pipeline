"""Greenhouse ATS scraper.

Greenhouse exposes a public JSON API at:
    https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs
    https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{job_id}

No auth needed — these are public job board endpoints.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseScraper(BaseScraper):
    """Scrape jobs from Greenhouse public API."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        """Scrape all matching jobs from a Greenhouse company board."""
        jobs: list[Job] = []
        url = f"{GREENHOUSE_API}/{company_slug}/jobs"

        async with httpx.AsyncClient(timeout=30) as client:
            # Fetch job listings
            resp = await client.get(url, params={"content": "true"})
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("jobs", []):
                title = item.get("title", "")
                location = item.get("location", {}).get("name", "")
                content = item.get("content", "")

                if not self.passes_filters(title, location, content):
                    continue

                job_id = str(item["id"])
                job = Job(
                    id=f"gh-{company_slug}-{job_id}",
                    title=title,
                    company=item.get("company", {}).get("name", company_slug),
                    company_slug=company_slug,
                    location=location,
                    url=item.get("absolute_url", f"https://boards.greenhouse.io/{company_slug}/jobs/{job_id}"),
                    platform=ATSPlatform.GREENHOUSE,
                    description=content,
                    department=_extract_department(item),
                    posted_at=_parse_date(item.get("updated_at")),
                )
                jobs.append(job)

        return jobs


def _extract_department(item: dict[str, Any]) -> str:
    """Extract department from Greenhouse job data."""
    departments = item.get("departments", [])
    if departments:
        return departments[0].get("name", "")
    return ""


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse Greenhouse date format."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
