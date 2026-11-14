"""Lever ATS scraper.

Lever exposes a public JSON API at:
    https://api.lever.co/v0/postings/{company_slug}?mode=json
    https://api.lever.co/v0/postings/{company_slug}/{posting_id}
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

LEVER_API = "https://api.lever.co/v0/postings"


class LeverScraper(BaseScraper):
    """Scrape jobs from Lever public API."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        """Scrape all matching jobs from a Lever company posting page."""
        url = f"{LEVER_API}/{company_slug}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params={"mode": "json"})
            resp.raise_for_status()
            return self.parse_listing(company_slug, resp.json())

    def parse_listing(self, company_slug: str, postings: Any, light: bool = False) -> list[Job]:
        if not isinstance(postings, list):
            logger.warning(f"Unexpected Lever response for {company_slug}")
            return []
        jobs: list[Job] = []
        for item in postings:
            title = item.get("text", "")
            categories = item.get("categories", {}) or {}
            location = categories.get("location", "") or ""
            department = categories.get("department", "")
            team = categories.get("team", "")
            desc = item.get("descriptionPlain", "") or item.get("description", "")
            if not self.passes_filters(title, location, desc):
                continue
            job_id = item.get("id", "")
            jobs.append(Job(
                id=f"lv-{company_slug}-{job_id}",
                title=title,
                company=company_slug.replace("-", " ").title(),
                company_slug=company_slug,
                location=location,
                url=item.get("hostedUrl", f"https://jobs.lever.co/{company_slug}/{job_id}"),
                platform=ATSPlatform.LEVER,
                description=desc,
                department=department or team,
                posted_at=_parse_timestamp(item.get("createdAt")),
            ))
        return jobs


def _parse_timestamp(ts: int | None) -> datetime | None:
    """Parse Lever's millisecond timestamp."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000)
    except (ValueError, TypeError, OSError):
        return None
