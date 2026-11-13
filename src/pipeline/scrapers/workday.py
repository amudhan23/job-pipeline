"""Workday ATS scraper.

Every Workday careers site (*.myworkdayjobs.com, *.myworkdaysite.com) is a
JavaScript app over the same keyless JSON API ("CXS"):

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}
    GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}   (one posting)

No login is needed to read jobs (applying needs an account). The API is
not a documented public contract, so parsing here is defensive.

A board is identified by a slug that encodes host + tenant + site:
    nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    wd5.myworkdaysite.com/microchiphr/External
The host is built from the slug, so slugs are validated against strict
patterns before any request (no host injection).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from pipeline.models import ATSPlatform, Job
from .base import BaseScraper

logger = logging.getLogger(__name__)

PAGE_SIZE = 20          # Workday's maximum page size
MAX_PAGES_PER_TERM = 2  # per search term; boards return the best matches first
MAX_DETAILS = 25        # detail calls per board per run

_SITE = r"[A-Za-z0-9_-]{1,80}"
_JOBS_URL = re.compile(
    rf"https?://(?P<tenant>[a-z0-9-]+)\.(?P<shard>wd\d+)\.myworkdayjobs\.com/"
    rf"(?:[a-z]{{2}}-[a-z]{{2}}/)?(?P<site>{_SITE})(?:[/?#]|$)", re.IGNORECASE)
_SITE_URL = re.compile(
    rf"https?://(?P<shard>wd\d+)\.myworkdaysite\.com/(?:[a-z]{{2}}-[a-z]{{2}}/)?"
    rf"recruiting/(?P<tenant>[a-z0-9-]+)/(?P<site>{_SITE})(?:[/?#]|$)", re.IGNORECASE)
_JOBS_SLUG = re.compile(rf"^(?P<tenant>[a-z0-9-]+)\.(?P<shard>wd\d+)\.myworkdayjobs\.com/(?P<site>{_SITE})$")
_SITE_SLUG = re.compile(rf"^(?P<shard>wd\d+)\.myworkdaysite\.com/(?P<tenant>[a-z0-9-]+)/(?P<site>{_SITE})$")

# Path words that are never a career-site name
_NOT_SITES = {"job", "jobs", "details", "wday", "login", "apply"}


@dataclass(frozen=True)
class WorkdayBoard:
    host: str
    tenant: str
    site: str
    via_myworkdaysite: bool = False

    @property
    def slug(self) -> str:
        if self.via_myworkdaysite:
            return f"{self.host}/{self.tenant}/{self.site}"
        return f"{self.host}/{self.site}"

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    def public_url(self, external_path: str) -> str:
        prefix = f"/recruiting/{self.tenant}" if self.via_myworkdaysite else ""
        return f"https://{self.host}{prefix}/{self.site}{external_path}"


def board_slug_from_url(url: str) -> str | None:
    """Registry slug for the Workday board a job URL belongs to."""
    if m := _JOBS_URL.match(url):
        if m["site"].lower() in _NOT_SITES:
            return None
        return f"{m['tenant'].lower()}.{m['shard'].lower()}.myworkdayjobs.com/{m['site']}"
    if m := _SITE_URL.match(url):
        if m["site"].lower() in _NOT_SITES:
            return None
        return f"{m['shard'].lower()}.myworkdaysite.com/{m['tenant'].lower()}/{m['site']}"
    return None


def parse_board(slug: str) -> WorkdayBoard | None:
    """Validate a registry slug and turn it back into a board. None if unsafe."""
    if m := _JOBS_SLUG.match(slug):
        return WorkdayBoard(f"{m['tenant']}.{m['shard']}.myworkdayjobs.com", m["tenant"], m["site"])
    if m := _SITE_SLUG.match(slug):
        return WorkdayBoard(f"{m['shard']}.myworkdaysite.com", m["tenant"], m["site"], True)
    return None


_POSTED_N = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def parse_posted_on(text: str | None, now: datetime | None = None) -> datetime | None:
    """Parse Workday's relative label: "Posted Today", "Posted Yesterday",
    "Posted 3 Days Ago", "Posted 30+ Days Ago"."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    if m := _POSTED_N.search(t):
        days = int(m.group(1))
        # "30+ days" means at least 30 — treat as just past it
        return now - timedelta(days=days + (1 if "+" in t else 0))
    return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_MULTI_LOCATION = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)


class WorkdayScraper(BaseScraper):
    """Scrape a Workday careers site through its public CXS API."""

    def _search_terms(self) -> list[str]:
        terms = [k for k in dict.fromkeys(self.filters.get("role_keywords", [])) if k]
        return terms or [""]

    async def _post(self, client: httpx.AsyncClient, url: str, body: dict) -> dict:
        """POST with a small retry for Workday's occasional 429/503."""
        for attempt in range(3):
            resp = await client.post(url, json=body)
            if resp.status_code in (429, 503) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        return {}

    async def scrape_company(self, company_slug: str) -> list[Job]:
        board = parse_board(company_slug)
        if not board:
            # Raise so the registry counts it as a failure and retires it
            raise ValueError(f"unsafe or malformed Workday slug: {company_slug!r}")

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            postings = await self._search(client, board)

            jobs: list[Job] = []
            details = 0
            for path, item in postings.items():
                title = item.get("title", "")
                if not self.passes_title(title):
                    continue
                # Skip the detail call when the list already shows a single
                # location that fails the filter (e.g. "US-AZ-Chandler")
                loc_hint = item.get("locationsText", "") or ""
                if loc_hint and not _MULTI_LOCATION.match(loc_hint) \
                        and not self.passes_filters(title, loc_hint):
                    continue
                if details >= MAX_DETAILS:
                    break
                details += 1
                job = await self._job_from_detail(client, board, path, item)
                if job and self.passes_filters(job.title, job.location, job.description):
                    jobs.append(job)
        return jobs

    async def _search(self, client: httpx.AsyncClient, board: WorkdayBoard) -> dict[str, dict]:
        """Search the board once per role keyword; dedupe by externalPath."""
        found: dict[str, dict] = {}
        for term in self._search_terms():
            total = None
            for page in range(MAX_PAGES_PER_TERM):
                data = await self._post(client, f"{board.api_base}/jobs", {
                    "appliedFacets": {}, "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE, "searchText": term,
                })
                items = data.get("jobPostings") or []
                # Workday only reports "total" on the first page
                if total is None:
                    total = data.get("total") or 0
                for item in items:
                    if item.get("externalPath"):
                        found.setdefault(item["externalPath"], item)
                if len(items) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= total:
                    break
        return found

    async def _job_from_detail(
        self, client: httpx.AsyncClient, board: WorkdayBoard, path: str, item: dict,
    ) -> Job | None:
        try:
            resp = await client.get(f"{board.api_base}{path}")
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            data = {}
        info = data.get("jobPostingInfo") or {}

        locations = [info.get("location")] + list(info.get("additionalLocations") or [])
        country = (info.get("country") or {}).get("descriptor")
        location = "; ".join(x for x in locations if x) or item.get("locationsText", "")
        if country and country.lower() not in location.lower():
            location = f"{location}; {country}" if location else country
        if (info.get("remoteType") or "").lower().startswith("remote") and "remote" not in location.lower():
            location = f"Remote; {location}" if location else "Remote"

        company = (data.get("hiringOrganization") or {}).get("name") \
            or board.tenant.replace("-", " ").title()
        posted_at = _parse_date(info.get("startDate")) or parse_posted_on(
            info.get("postedOn") or item.get("postedOn"))
        req_id = info.get("jobReqId") or (item.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]

        return Job(
            id=f"wd-{board.tenant}-{req_id}",
            title=info.get("title") or item.get("title", ""),
            company=company,
            company_slug=board.slug,
            location=location,
            url=info.get("externalUrl") or board.public_url(path),
            platform=ATSPlatform.WORKDAY,
            description=info.get("jobDescription", "") or "",
            posted_at=posted_at,
        )
