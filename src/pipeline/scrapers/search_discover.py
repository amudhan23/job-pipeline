"""Search-based ATS slug discovery.

Instead of maintaining a hardcoded list of company slugs, discover them
dynamically with search-engine `site:` queries — the technique from the
a16z talent post:

    site:jobs.ashbyhq.com ("software engineer" OR "backend engineer") "new york"

This finds EVERY company on an ATS, not just the ones you already know.
Each ATS hosts job pages on a predictable domain, so a `site:` search plus
a role/location filter surfaces live postings, and the company slug lives
in the URL path.

Two modes:
  1. generate_queries()  — build the boolean queries to paste into Google
                           manually (no API key needed).
  2. discover()          — execute them via a search API and extract slugs
                           automatically (needs SERPER_API_KEY or SERPAPI_KEY).
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── ATS domain → how to pull the company slug out of a result URL ──
# Each entry: the search domain, and a regex whose group(1) is the slug.
@dataclass
class ATSDomain:
    name: str               # our internal platform key (greenhouse/ashby/lever)
    domain: str             # the site: domain to search
    slug_pattern: str       # regex to extract slug from a URL
    scrapable: bool = True   # whether we have a scraper for it yet
    lowercase: bool = True   # whether slugs are case-insensitive (SmartRecruiters is NOT)


ATS_DOMAINS: list[ATSDomain] = [
    # Platforms we can scrape today
    ATSDomain("greenhouse", "boards.greenhouse.io", r"boards\.greenhouse\.io/([^/?#]+)"),
    ATSDomain("greenhouse", "job-boards.greenhouse.io", r"job-boards\.greenhouse\.io/([^/?#]+)"),
    ATSDomain("ashby", "jobs.ashbyhq.com", r"jobs\.ashbyhq\.com/([^/?#]+)"),
    ATSDomain("lever", "jobs.lever.co", r"jobs\.lever\.co/([^/?#]+)"),
    # SmartRecruiters slugs are case-sensitive (e.g. "Visa") — do not lowercase.
    ATSDomain("smartrecruiters", "jobs.smartrecruiters.com", r"jobs\.smartrecruiters\.com/([^/?#]+)", lowercase=False),
    ATSDomain("workable", "apply.workable.com", r"apply\.workable\.com/([^/?#]+)"),
    ATSDomain("bamboohr", "bamboohr.com", r"https?://([^.]+)\.bamboohr\.com"),
    # Platforms the a16z post lists — login/key required, scrapers are a follow-up
    ATSDomain("jobvite", "jobs.jobvite.com", r"jobs\.jobvite\.com/([^/?#]+)", scrapable=False),
    ATSDomain("icims", "careers.icims.com", r"careers\.icims\.com/([^/?#]+)", scrapable=False),
    ATSDomain("jazz", "apply.jazz.co", r"apply\.jazz\.co/([^/?#]+)", scrapable=False),
]


@dataclass
class DiscoveryResult:
    """Slugs discovered per ATS platform."""
    by_platform: dict[str, set[str]] = field(default_factory=dict)

    def add(self, platform: str, slug: str) -> None:
        self.by_platform.setdefault(platform, set()).add(slug)

    def total(self) -> int:
        return sum(len(s) for s in self.by_platform.values())


# Slugs that are platform infrastructure, never a real company board.
_SLUG_BLOCKLIST = {
    "embed", "api", "www", "static", "assets", "job-board", "job-boards",
    "postings", "v0", "v1", "jobs", "careers", "search", "help", "about",
}


def _build_role_clause(role_keywords: list[str]) -> str:
    """Build an OR clause of quoted role keywords."""
    if not role_keywords:
        return ""
    quoted = [f'"{kw}"' for kw in role_keywords]
    return "(" + " OR ".join(quoted) + ")"


def _build_location_clause(locations: list[str]) -> str:
    """Build an OR clause of quoted locations."""
    if not locations:
        return ""
    # Drop vague tokens that add noise
    useful = [loc for loc in locations if loc.lower() not in {"remote"}]
    if not useful:
        return ""
    quoted = [f'"{loc}"' for loc in useful]
    return "(" + " OR ".join(quoted) + ")"


def generate_queries(
    config: dict[str, Any],
    platforms: list[str] | None = None,
    include_location: bool = True,
    per_keyword: bool = False,
    scrapable_only: bool = False,
) -> list[tuple[str, str]]:
    """Generate `site:` boolean search queries from the config filters.

    Returns (platform, query) tuples. By default, one query per ATS domain
    with all role keywords OR'd together (handy to paste into a browser).
    With per_keyword=True, one query per (domain, role keyword) — each query
    surfaces a different set of results, so automated discovery covers far
    more companies.
    """
    filters = config.get("filters", {})
    role_keywords = filters.get("role_keywords", [])
    loc_clause = _build_location_clause(filters.get("locations", [])) if include_location else ""

    role_clauses = (
        [_build_role_clause([kw]) for kw in role_keywords] if per_keyword
        else [_build_role_clause(role_keywords)]
    ) or [""]

    queries: list[tuple[str, str]] = []
    seen_domains: set[str] = set()
    for ats in ATS_DOMAINS:
        if platforms and ats.name not in platforms:
            continue
        if scrapable_only and not ats.scrapable:
            continue
        if ats.domain in seen_domains:
            continue
        seen_domains.add(ats.domain)

        for role_clause in role_clauses:
            parts = [f"site:{ats.domain}"]
            if role_clause:
                parts.append(role_clause)
            if loc_clause:
                parts.append(loc_clause)
            queries.append((ats.name, " ".join(parts)))

    return queries


def _extract_slug(url: str) -> tuple[str, str] | None:
    """Extract (platform, slug) from a result URL, or None."""
    for ats in ATS_DOMAINS:
        m = re.search(ats.slug_pattern, url)
        if m:
            slug = m.group(1).strip()
            # Case-insensitive platforms normalize to lowercase; SmartRecruiters
            # keeps its original case because its API slugs are case-sensitive.
            key = slug.lower() if ats.lowercase else slug
            if key and key.lower() not in _SLUG_BLOCKLIST:
                return ats.name, key
    return None


class RateLimited(Exception):
    """The search backend is throttling us; stop searching for this run."""


# recency: None | "day" | "week" | "month" | "year"
_DDG_RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}
_GOOGLE_RECENCY = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


async def _search_serper(query: str, api_key: str, recency: str | None = None) -> list[str]:
    """Run a query via Serper.dev (Google results). Returns result URLs."""
    payload: dict[str, Any] = {"q": query, "num": 100}
    if recency in _GOOGLE_RECENCY:
        payload["tbs"] = _GOOGLE_RECENCY[recency]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 429:
            raise RateLimited("serper")
        resp.raise_for_status()
        return [i["link"] for i in resp.json().get("organic", []) if "link" in i]


async def _search_serpapi(query: str, api_key: str, recency: str | None = None) -> list[str]:
    """Run a query via SerpAPI (Google results). Returns result URLs."""
    params: dict[str, Any] = {"q": query, "num": 100, "api_key": api_key, "engine": "google"}
    if recency in _GOOGLE_RECENCY:
        params["tbs"] = _GOOGLE_RECENCY[recency]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get("https://serpapi.com/search", params=params)
        if resp.status_code == 429:
            raise RateLimited("serpapi")
        resp.raise_for_status()
        return [i["link"] for i in resp.json().get("organic_results", []) if "link" in i]


# Matches result links in DuckDuckGo HTML output.
_DDG_LINK_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"')
# DDG wraps external links in a redirect: //duckduckgo.com/l/?uddg=<encoded>
_DDG_REDIRECT_RE = re.compile(r"[?&]uddg=([^&]+)")


def _clean_ddg_url(href: str) -> str:
    """Unwrap a DuckDuckGo redirect link into the real target URL."""
    href = html.unescape(href)
    m = _DDG_REDIRECT_RE.search(href)
    if m:
        return unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_ddg_html(body: str) -> list[str]:
    """Extract result URLs from a DuckDuckGo HTML response."""
    return [_clean_ddg_url(h) for h in _DDG_LINK_RE.findall(body)]


async def _search_duckduckgo(query: str, _key: str = "", recency: str | None = None) -> list[str]:
    """Run a query via DuckDuckGo's HTML endpoint. Free, no API key.

    This is the default backend — "doing the Serper thing ourselves."
    DDG supports the `site:` operator, and the `df` field restricts results
    to pages from the past day/week/month/year.
    """
    data = {"q": query}
    cookies = {}
    if recency in _DDG_RECENCY:
        data["df"] = cookies["df"] = _DDG_RECENCY[recency]
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data=data,
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                )
            },
        )
        # DDG signals throttling with a 202 or a bot-check ("anomaly") page
        if resp.status_code in (202, 429) or "anomaly-modal" in resp.text:
            raise RateLimited("duckduckgo")
        resp.raise_for_status()
        return parse_ddg_html(resp.text)


def _get_search_backend() -> tuple[str, str]:
    """Pick a search backend. Paid APIs win if keys are set; else free DDG."""
    if key := os.environ.get("SERPER_API_KEY"):
        return "serper", key
    if key := os.environ.get("SERPAPI_KEY"):
        return "serpapi", key
    return "duckduckgo", ""


async def discover(
    config: dict[str, Any],
    platforms: list[str] | None = None,
    include_location: bool = True,
    recency: str | None = "week",
    delay: float = 1.5,
    max_queries: int | None = None,
) -> DiscoveryResult:
    """Discover company boards by searching the web for recent job postings.

    Runs one `site:` query per (ATS domain, role keyword), restricted to
    pages from the last `recency` window, and pulls the company slug out of
    every result URL. Uses DuckDuckGo for free by default; SERPER_API_KEY or
    SERPAPI_KEY switch to Google results.

    If the backend starts throttling, discovery stops early and returns
    what it found — the run continues with the boards already known.
    """
    import asyncio

    backend_name, api_key = _get_search_backend()
    search_fn = {
        "serper": _search_serper,
        "serpapi": _search_serpapi,
        "duckduckgo": _search_duckduckgo,
    }[backend_name]

    queries = generate_queries(
        config, platforms, include_location, per_keyword=True, scrapable_only=True,
    )
    if max_queries:
        queries = queries[:max_queries]
    logger.info(f"Web discovery: {len(queries)} queries via {backend_name} (recency={recency})")

    result = DiscoveryResult()
    for i, (platform, query) in enumerate(queries):
        if i and delay:
            await asyncio.sleep(delay)
        try:
            urls = await search_fn(query, api_key, recency)
        except RateLimited:
            logger.warning(
                f"{backend_name} is rate-limiting — stopping web search after "
                f"{i}/{len(queries)} queries. Known boards will still be scraped."
            )
            break
        except httpx.HTTPError as e:
            logger.error(f"Search failed for {platform}: {e}")
            continue
        for url in urls:
            extracted = _extract_slug(url)
            if extracted:
                result.add(*extracted)
        logger.debug(f"[{platform}] {len(urls)} results for: {query}")

    logger.info(f"Web discovery found {result.total()} boards")
    return result
