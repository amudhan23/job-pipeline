"""Pipeline orchestrator — coordinates all stages.

The pipeline is web-first. It never needs to know a company in advance:

  1. Discover   — search the web for recent postings on every public ATS
                  (site: queries) and pull job feeds (SimplifyJobs). Every
                  company board found is recorded in the board registry.
  2. Scrape     — scrape every active board in the registry, concurrently.
                  A company that posted its first job yesterday was found in
                  step 1 and gets scraped here.
  3. Merge      — add feed jobs (incl. Workday/iCIMS roles we can't scrape
                  directly), dedupe by URL.
  4. Filter/score/track — sponsorship check, fit score, save.

The `ats:` lists in config are optional pins — companies you always want
checked even if they don't show up in discovery. They are not required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pipeline.discovery import BoardRegistry, SimplifyFeed, normalize_url
from pipeline.discovery.registry import pretty_slug
from pipeline.models import Job
from pipeline.scrapers import (
    GreenhouseScraper, AshbyScraper, LeverScraper,
    SmartRecruitersScraper, WorkableScraper, BambooHRScraper, WorkdayScraper,
)
from pipeline.scrapers.search_discover import discover as web_discover
from pipeline.filters import SponsorshipFilter
from pipeline.scoring import FitScorer
from pipeline.tracker import Tracker
from pipeline.utils.dates import job_age  # noqa: F401  (re-exported for the CLI)

logger = logging.getLogger(__name__)

RECENCY_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


@dataclass
class RunStats:
    """What a run found — shown by the CLI."""
    new_boards: dict[str, int] = field(default_factory=dict)   # source -> count
    boards_scraped: dict[str, int] = field(default_factory=dict)  # platform -> count
    boards_failed: int = 0
    jobs_from_boards: int = 0
    jobs_from_feeds: int = 0
    duplicates_dropped: int = 0
    closed_dropped: int = 0      # feed jobs missing from their (successfully scraped) board
    max_age_days: int | None = None
    too_old_dropped: int = 0
    undated_dropped: int = 0
    search: Any = None  # DiscoveryResult diagnostics from web search


class Pipeline:
    """Orchestrate the full job application pipeline."""

    def __init__(self, config: dict[str, Any], registry: BoardRegistry | None = None) -> None:
        self.config = config
        self.discovery_cfg = config.get("discovery", {})
        self.tracker = Tracker()
        self.registry = registry or BoardRegistry()
        self.stats = RunStats()

        self.scrapers = {
            "greenhouse": GreenhouseScraper(config),
            "ashby": AshbyScraper(config),
            "lever": LeverScraper(config),
            "smartrecruiters": SmartRecruitersScraper(config),
            "workable": WorkableScraper(config),
            "bamboohr": BambooHRScraper(config),
            "workday": WorkdayScraper(config),
        }
        self.sponsorship = SponsorshipFilter(
            blocklist=config.get("sponsorship_blocklist", []),
            enabled=config.get("sponsorship_filter_enabled", True),
        )
        self.scorer = FitScorer(config)

    # ── Stage 1: discovery ──────────────────────────────────────────────

    def _register_pins(self) -> None:
        """Optional companies pinned in config — always checked."""
        for platform, slugs in (self.config.get("ats") or {}).items():
            if platform in self.scrapers:
                for slug in slugs or []:
                    self.registry.add(platform, slug, source="pinned")

    async def _discover_web(self, recency: str | None) -> None:
        result = await web_discover(
            self.config,
            include_location=self.discovery_cfg.get("include_location", True),
            recency=recency,
            delay=self.discovery_cfg.get("search_delay", 2.0),
            max_queries=self.discovery_cfg.get("max_queries"),
        )
        boards = {(p, s) for p, slugs in result.by_platform.items() for s in slugs}
        new = self.registry.add_many(boards, source="web")
        self.stats.new_boards["web search"] = new
        self.stats.search = result

    async def _discover_feeds(self) -> list[Job]:
        feed_cfg = self.discovery_cfg.get("feeds", {})
        if not feed_cfg.get("simplify_new_grad", True):
            return []
        feed = SimplifyFeed(
            max_age_days=self.discovery_cfg.get("feed_max_age_days", 30),
            categories=feed_cfg.get("simplify_categories", ["Software", "AI/ML/Data"]),
        )
        # Any scraper's passes_filters works — they share the config filters
        try:
            jobs, boards = await feed.collect(next(iter(self.scrapers.values())).passes_filters)
        except Exception as e:
            logger.error(f"Simplify feed failed: {e}")
            return []
        self.stats.new_boards["job feeds"] = self.registry.add_many(boards, source="feed")
        self.registry.set_names(feed.board_names)
        self.stats.jobs_from_feeds = len(jobs)
        return jobs

    def _apply_company_names(self, jobs: list[Job]) -> None:
        """Board APIs often return no company name, leaving the raw slug
        ("kalshi"). Use the name a feed gave us, else a readable slug."""
        names = self.registry.names()
        for job in jobs:
            known = names.get((job.platform.value, job.company_slug))
            if known:
                job.company = known
            elif job.company == job.company_slug:
                # Exactly the raw slug — the API gave no name. (A real name
                # like "FeedCo" whose slug is "feedco" is left alone.)
                job.company = pretty_slug(job.company_slug)

    # ── Stage 2: scrape every known board ───────────────────────────────

    async def _scrape_registry(self) -> list[Job]:
        concurrency = self.discovery_cfg.get("concurrency", 8)
        all_jobs: list[Job] = []
        for platform, scraper in self.scrapers.items():
            slugs = self.registry.slugs(platform)
            if not slugs:
                continue
            jobs, status = await scraper.scrape_many(slugs, concurrency=concurrency)
            for slug, count in status.items():
                self.registry.record_result(platform, slug, count)
                if count is not None:
                    self._scraped_ok.add((platform, slug))
            failed = sum(1 for c in status.values() if c is None)
            self.stats.boards_scraped[platform] = len(slugs)
            self.stats.boards_failed += failed
            all_jobs.extend(jobs)
            logger.info(f"{platform}: {len(jobs)} jobs from {len(slugs)} boards ({failed} failed)")
        self.stats.jobs_from_boards = len(all_jobs)
        return all_jobs

    # ── Stage 3: merge ──────────────────────────────────────────────────

    def _merge(self, board_jobs: list[Job], feed_jobs: list[Job]) -> list[Job]:
        """Combine board + feed jobs. Board versions win (they have the full JD)."""
        merged: dict[str, Job] = {}
        seen_titles: set[tuple[str, str, str]] = set()
        for job in board_jobs:
            merged.setdefault(normalize_url(job.url), job)
            seen_titles.add((job.platform.value, job.company_slug, job.title.lower()))

        dropped = len(board_jobs) - len(merged)
        for job in feed_jobs:
            key = normalize_url(job.url)
            # Same posting already scraped from the board (URL may differ:
            # some boards link to the company's own careers domain)
            title_key = (job.platform.value, job.company_slug, job.title.lower())
            if key in merged or title_key in seen_titles:
                dropped += 1
                continue
            # We just read this company's board and the job isn't on it:
            # the feed is stale and the posting has closed.
            # (Not Workday: it's searched with page limits, so absence isn't proof.)
            if job.platform.value != "workday" and \
                    (job.platform.value, job.company_slug) in self._scraped_ok:
                self.stats.closed_dropped += 1
                continue
            merged[key] = job
        self.stats.duplicates_dropped = dropped
        return list(merged.values())

    # ── Stage 4: freshness ──────────────────────────────────────────────

    def _filter_age(self, jobs: list[Job], max_age_days: int | None) -> list[Job]:
        """Keep jobs posted within max_age_days. Undated jobs are dropped
        when a window is set — we can't claim they're recent."""
        self.stats.max_age_days = max_age_days
        if not max_age_days:
            return jobs
        window = timedelta(days=max_age_days)
        kept = []
        for job in jobs:
            age = job_age(job)
            if age is None:
                self.stats.undated_dropped += 1
            elif age > window:
                self.stats.too_old_dropped += 1
            else:
                kept.append(job)
        return kept

    # ── Public entry points ─────────────────────────────────────────────

    async def run_scrape(
        self, discover: bool = True, recency: str | None = None,
        max_age_days: int | None = None,
    ) -> list[Job]:
        """Discover boards from the web, scrape everything known, keep fresh jobs.

        recency (day/week/month/year), when given, limits BOTH the web search
        and the jobs returned to that window. max_age_days overrides the job
        window; with neither, config `filters.max_job_age_days` applies.
        """
        self.stats = RunStats()
        self._scraped_ok: set[tuple[str, str]] = set()
        search_recency = recency or self.discovery_cfg.get("recency", "week")
        if max_age_days is None:
            max_age_days = (
                RECENCY_DAYS[recency] if recency
                else self.config.get("filters", {}).get("max_job_age_days")
            )
        self._register_pins()

        feed_jobs: list[Job] = []
        if discover and self.discovery_cfg.get("enabled", True):
            await self._discover_web(search_recency)
            feed_jobs = await self._discover_feeds()

        board_jobs = await self._scrape_registry()
        jobs = self._merge(board_jobs, feed_jobs)
        jobs = self._filter_age(jobs, max_age_days)
        self._apply_company_names(jobs)
        logger.info(f"Total: {len(jobs)} jobs ({self.stats.duplicates_dropped} duplicates dropped)")
        return jobs

    async def run_full(
        self, discover: bool = True, recency: str | None = None,
        max_age_days: int | None = None,
    ) -> list[Job]:
        """Run the full pipeline: discover → scrape → filter → score → track."""
        jobs = await self.run_scrape(discover=discover, recency=recency, max_age_days=max_age_days)
        if jobs:
            jobs = self.sponsorship.enrich_jobs(jobs)
            jobs = self.sponsorship.filter_blocked(jobs)
            logger.info(f"After sponsorship: {len(jobs)} eligible jobs")
            jobs = self.scorer.score_jobs(jobs)
        else:
            logger.warning("No jobs found")

        # Record the run even when empty, so "latest run" views never show
        # stale results from an earlier run.
        run_id = self.tracker.start_run(self.stats.max_age_days)
        self.tracker.upsert_jobs(jobs, run_id=run_id)
        self.tracker.finish_run(run_id, len(jobs))
        self.run_id = run_id
        return jobs
