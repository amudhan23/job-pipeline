"""How to read one board cheaply.

Greenhouse, Lever and Ashby have a single listing URL per board, so they
get the full treatment: conditional GET, body hash (skip parsing when
nothing changed), and — for Greenhouse — a light listing without
descriptions, fetching a job's description only when it's new to us.

Platforms that need a request per job (Workday, SmartRecruiters, BambooHR,
Workable) use their existing scrapers on a slower schedule.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

from pipeline.crawler.fetcher import Fetcher, FetchResult
from pipeline.models import Job
from pipeline.scrapers.ashby import ASHBY_API
from pipeline.scrapers.greenhouse import GREENHOUSE_API
from pipeline.scrapers.lever import LEVER_API


@dataclass
class Listing:
    status: int                      # HTTP status (0 = network error, 304 = unchanged)
    jobs: list[Job] = field(default_factory=list)   # relevant postings on the board now
    unchanged: bool = False          # 304 or same body as last time — nothing to do
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    error: str = ""
    complete: bool = True            # False: absence from `jobs` isn't proof a job closed


def _validators(r: FetchResult) -> tuple[str | None, str | None]:
    h = {k.lower(): v for k, v in r.headers.items()}
    return h.get("etag"), h.get("last-modified")


class ListingSource:
    """A board with one listing URL. Subclasses say where and how to parse."""
    light = False

    def __init__(self, scraper) -> None:
        self.scraper = scraper

    def url(self, slug: str) -> tuple[str, dict | None]:
        raise NotImplementedError

    async def read(self, fetcher: Fetcher, slug: str, board: dict,
                   known: dict[str, str | None]) -> Listing:
        url, params = self.url(slug)
        r = await fetcher.get(url, params=params, etag=board.get("etag"),
                              last_modified=board.get("last_modified"))
        etag, lm = _validators(r)
        if r.not_modified:
            return Listing(304, unchanged=True, etag=board.get("etag"),
                           last_modified=board.get("last_modified"), content_hash=board.get("content_hash"))
        if not r.ok:
            return Listing(r.status, error=r.error or f"HTTP {r.status}")
        digest = hashlib.sha256(r.body).hexdigest()
        if digest == board.get("content_hash") and board.get("bootstrapped"):
            return Listing(r.status, unchanged=True, etag=etag, last_modified=lm, content_hash=digest)
        try:
            data = r.json()
        except ValueError:
            return Listing(r.status, error="not JSON")
        jobs = self.scraper.parse_listing(slug, data, light=self.light)
        jobs = await self.complete_new(fetcher, slug, jobs, known)
        return Listing(r.status, jobs=jobs, etag=etag, last_modified=lm, content_hash=digest)

    async def complete_new(self, fetcher: Fetcher, slug: str, jobs: list[Job],
                           known: dict[str, str | None]) -> list[Job]:
        return jobs


class GreenhouseSource(ListingSource):
    light = True
    MAX_DETAILS = 30          # a brand-new board can have hundreds; the rest wait a visit

    def url(self, slug: str) -> tuple[str, dict | None]:
        return f"{GREENHOUSE_API}/{slug}/jobs", None

    async def complete_new(self, fetcher: Fetcher, slug: str, jobs: list[Job],
                           known: dict[str, str | None]) -> list[Job]:
        """Fetch descriptions for postings we haven't stored yet (known ones
        keep the description saved on first sight)."""
        fresh = [j for j in jobs if j.id not in known]
        later = {j.id for j in fresh[self.MAX_DETAILS:]}
        fresh = fresh[:self.MAX_DETAILS]

        async def detail(job: Job) -> Job | None:
            gh_id = job.id.rsplit("-", 1)[-1]
            r = await fetcher.get(f"{GREENHOUSE_API}/{slug}/jobs/{gh_id}")
            if not r.ok:
                return job if job.location else None      # keep what we have
            try:
                item = r.json()
            except ValueError:
                return job
            full = self.scraper.job_from_item(slug, item)
            if not self.scraper.passes_filters(full.title, full.location, full.description):
                return None
            return full

        details = await asyncio.gather(*(detail(j) for j in fresh))
        by_id = {j.id: j for j in details if j is not None}
        out = []
        for j in jobs:
            if j.id in later:
                continue                    # not seen yet as far as the tracker knows
            if j.id in known:
                out.append(j)
            elif j.id in by_id:
                out.append(by_id[j.id])
        return out


class LeverSource(ListingSource):
    def url(self, slug: str) -> tuple[str, dict | None]:
        return f"{LEVER_API}/{slug}", {"mode": "json"}


class AshbySource(ListingSource):
    def url(self, slug: str) -> tuple[str, dict | None]:
        return f"{ASHBY_API}/{slug}", None


class ScraperSource:
    """Fallback: the platform's own scraper (one request per job)."""

    def __init__(self, scraper, timeout: float = 180.0) -> None:
        self.scraper, self.timeout = scraper, timeout

    async def read(self, fetcher: Fetcher, slug: str, board: dict,
                   known: dict[str, str | None]) -> Listing:
        try:
            jobs = await asyncio.wait_for(self.scraper.scrape_company(slug), self.timeout)
        except asyncio.TimeoutError:
            return Listing(0, error="timed out")
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0) or 0
            return Listing(status, error=f"{type(e).__name__}: {e}"[:200])
        # Workday is searched with page limits, so a missing job isn't proof it closed
        return Listing(200, jobs=jobs, complete=getattr(self.scraper, "complete_listing", True))


def build_sources(scrapers: dict[str, Any]) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for name, scraper in scrapers.items():
        if name == "greenhouse":
            sources[name] = GreenhouseSource(scraper)
        elif name == "lever":
            sources[name] = LeverSource(scraper)
        elif name == "ashby":
            sources[name] = AshbySource(scraper)
        else:
            sources[name] = ScraperSource(scraper)
    return sources
