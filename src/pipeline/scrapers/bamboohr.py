"""BambooHR ATS scraper.

BambooHR careers pages are served by an undocumented-but-public JSON feed
(no auth) that its own careers widget calls:
    https://{slug}.bamboohr.com/careers/list          -> job summaries
    https://{slug}.bamboohr.com/careers/{id}/detail   -> full posting body

The slug becomes part of the HOSTNAME, so it must be sanitized: an
unescaped "/" or "@" in the slug would silently retarget the request to
another host (e.g. "evil.com/x" -> https://evil.com/x.bamboohr.com/...).
We only allow [a-z0-9-] slugs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_DETAIL_FETCHES = 50  # cap N+1 detail calls per company


def _safe_slug(slug: str) -> str | None:
    """Validate a BambooHR subdomain slug. Returns the slug or None if unsafe."""
    s = slug.strip().lower()
    return s if _SLUG_RE.match(s) else None


class BambooHRScraper(BaseScraper):
    """Scrape jobs from a BambooHR company careers feed."""

    async def scrape_company(self, company_slug: str) -> list[Job]:
        slug = _safe_slug(company_slug)
        if not slug:
            logger.warning(f"Skipping unsafe BambooHR slug: {company_slug!r}")
            return []

        jobs: list[Job] = []
        base = f"https://{slug}.bamboohr.com/careers"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/list",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            detail_count = 0
            for item in data.get("result", []):
                title = item.get("jobOpeningName", "")
                location = _format_location(item)

                if not self.passes_filters(title, location):
                    continue

                job_id = str(item.get("id", ""))
                description = ""
                if detail_count < MAX_DETAIL_FETCHES:
                    description = await self._fetch_description(client, base, job_id)
                    detail_count += 1

                job = Job(
                    id=f"bamboo-{slug}-{job_id}",
                    title=title,
                    company=item.get("companyName", slug),
                    company_slug=slug,
                    location=location,
                    url=f"{base}/{job_id}",
                    platform=ATSPlatform.BAMBOOHR,
                    description=description,
                    department=item.get("departmentLabel", "") or "",
                    posted_at=None,
                )
                jobs.append(job)

        return jobs

    async def _fetch_description(
        self, client: httpx.AsyncClient, base: str, job_id: str
    ) -> str:
        """Fetch the full posting detail for the job description."""
        try:
            resp = await client.get(
                f"{base}/{job_id}/detail",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            detail = resp.json().get("result", {})
            return _extract_description(detail)
        except httpx.HTTPError:
            return ""


def _format_location(item: dict[str, Any]) -> str:
    """Build a location string from BambooHR location fields."""
    if item.get("isRemote"):
        return "Remote"
    loc = item.get("location", {})
    if isinstance(loc, dict):
        parts = [loc.get("city", ""), loc.get("state", ""), loc.get("country", "")]
        return ", ".join(p.strip() for p in parts if p and p.strip())
    return ""


def _extract_description(detail: dict[str, Any]) -> str:
    """Pull description text from a BambooHR posting detail payload."""
    parts = []
    for key in ("jobOpeningShareDescription", "description", "descriptionText"):
        text = detail.get(key, "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)
