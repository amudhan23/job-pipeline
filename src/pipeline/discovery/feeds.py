"""Job-first feeds — sources that list jobs across many companies at once.

A feed doesn't need to know any company in advance. It returns jobs
directly, and every job link that points at a scrapable ATS board also
registers that board, so the next stage can scrape the company's full
board (catching roles the feed missed).

SimplifyJobs publishes its curated listings as one JSON file on GitHub,
updated daily, with fields:
    company_name, title, url, locations[], date_posted (unix), active,
    is_visible, category, sponsorship, degrees, id, ...
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from pipeline.models import ATSPlatform, Job
from pipeline.scrapers.search_discover import ATS_DOMAINS, _extract_slug

logger = logging.getLogger(__name__)

SIMPLIFY_NEW_GRAD = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)

# Simplify sponsorship labels that mean "don't apply if you need a visa"
_BLOCKING_SPONSORSHIP = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}

_PLATFORM_BY_NAME = {p.value: p for p in ATSPlatform}
_SCRAPABLE = {a.name for a in ATS_DOMAINS if a.scrapable}


def normalize_url(url: str) -> str:
    """Canonical form for dedupe: lowercase host, no query/fragment/trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, "", ""))


def _platform_for(url: str) -> tuple[ATSPlatform, str | None]:
    """Work out which ATS a job URL belongs to, and its board slug if scrapable."""
    extracted = _extract_slug(url)
    if extracted:
        name, slug = extracted
        if name in _SCRAPABLE:
            return _PLATFORM_BY_NAME[name], slug
        # Known ATS we can't scrape yet (Jobvite, iCIMS...) — keep the job, no board
        return ATSPlatform.OTHER, None
    host = urlsplit(url).netloc.lower()
    if "myworkdayjobs" in host:
        return ATSPlatform.WORKDAY, None
    return ATSPlatform.OTHER, None


class SimplifyFeed:
    """Pull roles from a SimplifyJobs listings.json feed."""

    def __init__(
        self,
        url: str = SIMPLIFY_NEW_GRAD,
        max_age_days: int = 30,
        categories: list[str] | None = None,
    ) -> None:
        self.url = url
        self.max_age_days = max_age_days
        self.categories = set(categories) if categories else None

    async def fetch(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            return resp.json()

    def parse(
        self, listings: list[dict[str, Any]], scraper_filter
    ) -> tuple[list[Job], set[tuple[str, str]]]:
        """Turn raw listings into Jobs plus the set of ATS boards they point at.

        scraper_filter: a callable(title, location) -> bool, normally
        BaseScraper.passes_filters, so feed jobs get the same role/location rules.
        """
        cutoff = time.time() - self.max_age_days * 86400
        jobs: list[Job] = []
        boards: set[tuple[str, str]] = set()

        for item in listings:
            if not (item.get("active") and item.get("is_visible", True)):
                continue
            if item.get("date_posted", 0) < cutoff:
                continue
            if self.categories and item.get("category") not in self.categories:
                continue

            url = item.get("url", "")
            if not url:
                continue
            platform, slug = _platform_for(url)
            if slug:
                # Register the company's board even if this particular role is
                # filtered out — other roles on that board may fit.
                boards.add((platform.value, slug))

            title = item.get("title", "")
            location = "; ".join(item.get("locations") or [])
            if not scraper_filter(title, location):
                continue

            company = item.get("company_name", "")
            jobs.append(Job(
                id=f"simplify-{item.get('id', url)}",
                title=title,
                company=company,
                company_slug=slug or company.lower().replace(" ", "-"),
                location=location,
                url=url,
                platform=platform,
                description="",  # feed has no JD; board scrape fills it in if scrapable
                posted_at=datetime.fromtimestamp(item["date_posted"]) if item.get("date_posted") else None,
                jd_blocks_sponsorship=item.get("sponsorship") in _BLOCKING_SPONSORSHIP,
            ))

        return jobs, boards

    async def collect(self, scraper_filter) -> tuple[list[Job], set[tuple[str, str]]]:
        listings = await self.fetch()
        jobs, boards = self.parse(listings, scraper_filter)
        logger.info(
            f"Simplify feed: {len(jobs)} matching jobs, {len(boards)} ATS boards "
            f"(from {len(listings)} listings)"
        )
        return jobs, boards
