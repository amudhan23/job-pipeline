"""SmartRecruiters ATS scraper.

SmartRecruiters exposes a public Posting API (no auth):
    https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100
    https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}

The list endpoint returns summaries with pagination; full descriptions
live on the per-posting detail endpoint. Company slugs are case-sensitive.
Note: the public feed is tier-dependent — some companies have it disabled
(returns 403/404), which we handle gracefully.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies"
PAGE_LIMIT = 100
MAX_PAGES = 10  # safety cap: 1000 postings per company


class SmartRecruitersScraper(BaseScraper):
    """Scrape jobs from the SmartRecruiters public Posting API."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        jobs: list[Job] = []
        base = f"{SMARTRECRUITERS_API}/{company_slug}/postings"

        async with httpx.AsyncClient(timeout=30) as client:
            offset = 0
            for _ in range(MAX_PAGES):
                resp = await client.get(base, params={"limit": PAGE_LIMIT, "offset": offset})
                resp.raise_for_status()
                data = resp.json()
                postings = data.get("content", [])
                if not postings:
                    break

                for item in postings:
                    title = item.get("name", "")
                    location = _format_location(item.get("location", {}))
                    # List endpoint has no description; filter on title/location first,
                    # then fetch detail only for matches (keeps request count low).
                    if not self.passes_filters(title, location):
                        continue

                    job_id = str(item.get("id", ""))
                    description = await self._fetch_description(client, company_slug, job_id)

                    company_name = item.get("company", {}).get("name", company_slug)
                    job = Job(
                        id=f"sr-{company_slug}-{job_id}",
                        title=title,
                        company=company_name,
                        company_slug=company_slug,
                        location=location,
                        url=item.get("ref", f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}"),
                        platform=ATSPlatform.SMARTRECRUITERS,
                        description=description,
                        department=item.get("department", {}).get("label", ""),
                        posted_at=_parse_date(item.get("releasedDate")),
                    )
                    jobs.append(job)

                total = data.get("totalFound", 0)
                offset += PAGE_LIMIT
                if offset >= total:
                    break

        return jobs

    async def _fetch_description(
        self, client: httpx.AsyncClient, company_slug: str, job_id: str
    ) -> str:
        """Fetch the full posting detail for the job description."""
        try:
            resp = await client.get(f"{SMARTRECRUITERS_API}/{company_slug}/postings/{job_id}")
            resp.raise_for_status()
            detail = resp.json()
            return _extract_description(detail)
        except httpx.HTTPError:
            return ""


def _format_location(loc: dict[str, Any]) -> str:
    """Build a location string from SmartRecruiters location fields."""
    if loc.get("remote"):
        return "Remote"
    parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
    return ", ".join(p for p in parts if p)


def _extract_description(detail: dict[str, Any]) -> str:
    """Pull job description text out of the posting detail payload."""
    sections = detail.get("jobAd", {}).get("sections", {})
    chunks = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key, {})
        text = section.get("text", "")
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
