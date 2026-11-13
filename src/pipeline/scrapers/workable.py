"""Workable ATS scraper.

Workable exposes a public widget API (no auth) that returns every open
role for a company — including the full description — in one call:
    https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Company slug is the account name in apply.workable.com/{slug}.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts"


class WorkableScraper(BaseScraper):
    """Scrape jobs from the Workable public widget API."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        jobs: list[Job] = []
        url = f"{WORKABLE_API}/{company_slug}"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, params={"details": "true"})
            resp.raise_for_status()
            data = resp.json()

            company_name = data.get("name", company_slug)
            for item in data.get("jobs", []):
                title = item.get("title", "")
                location = _format_location(item)
                description = item.get("description", "") or ""

                if not self.passes_filters(title, location, description):
                    continue

                shortcode = item.get("shortcode", "")
                job_url = item.get("url") or item.get("application_url") or \
                    f"https://apply.workable.com/{company_slug}/j/{shortcode}/"

                job = Job(
                    id=f"wk-{company_slug}-{shortcode}",
                    title=title,
                    company=company_name,
                    company_slug=company_slug,
                    location=location,
                    url=job_url,
                    platform=ATSPlatform.WORKABLE,
                    description=description,
                    department=item.get("department", "") or "",
                    posted_at=_parse_date(item.get("published_on") or item.get("created_at")),
                )
                jobs.append(job)

        return jobs


def _format_location(item: dict[str, Any]) -> str:
    """Build a location string from Workable job fields."""
    if item.get("remote") or item.get("telecommuting"):
        return "Remote"
    # Workable puts location under various shapes; try the common ones
    loc = item.get("location", {})
    if isinstance(loc, dict):
        parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        formatted = ", ".join(p for p in parts if p)
        if formatted:
            return formatted
    # Flat fields fallback
    parts = [item.get("city", ""), item.get("state", ""), item.get("country", "")]
    return ", ".join(p for p in parts if p)


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
