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

    LIST_URL = GREENHOUSE_API + "/{slug}/jobs"

    async def scrape_company(self, company_slug: str) -> list[Job]:
        """Scrape all matching jobs from a Greenhouse company board."""
        url = f"{GREENHOUSE_API}/{company_slug}/jobs"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params={"content": "true"})
            resp.raise_for_status()
            return self.parse_listing(company_slug, resp.json())

    def parse_listing(self, company_slug: str, data: dict[str, Any], light: bool = False) -> list[Job]:
        """Jobs from a board listing. light=True: the listing has no
        descriptions (the cheap poll) — jobs with no location are kept so the
        detail read can decide."""
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            title = item.get("title", "")
            location = (item.get("location") or {}).get("name", "")
            content = item.get("content", "")
            if light and not location.strip():
                if not self.passes_title(title):
                    continue
            elif not self.passes_filters(title, location, content):
                continue
            jobs.append(self.job_from_item(company_slug, item))
        return jobs

    def job_from_item(self, company_slug: str, item: dict[str, Any]) -> Job:
        job_id = str(item["id"])
        return Job(
            id=f"gh-{company_slug}-{job_id}",
            title=item.get("title", ""),
            company=item.get("company_name")
            or (item.get("company") or {}).get("name")
            or company_slug,
            company_slug=company_slug,
            location=(item.get("location") or {}).get("name", ""),
            url=item.get("absolute_url", f"https://boards.greenhouse.io/{company_slug}/jobs/{job_id}"),
            platform=ATSPlatform.GREENHOUSE,
            description=item.get("content", "") or "",
            department=_extract_department(item),
            # first_published = when the job went live; updated_at changes
            # on any edit, so it's only a fallback
            posted_at=_parse_date(item.get("first_published") or item.get("updated_at")),
        )


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
