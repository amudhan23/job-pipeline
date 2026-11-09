"""Pipeline orchestrator — coordinates all stages."""

from __future__ import annotations

import logging
from typing import Any

from pipeline.models import Job
from pipeline.scrapers import GreenhouseScraper, AshbyScraper, LeverScraper
from pipeline.scrapers.discover import KNOWN_GREENHOUSE, KNOWN_ASHBY, KNOWN_LEVER
from pipeline.filters import SponsorshipFilter
from pipeline.scoring import FitScorer
from pipeline.tracker import Tracker

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrate the full job application pipeline."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tracker = Tracker()

        # Initialize stages
        self.greenhouse = GreenhouseScraper(config)
        self.ashby = AshbyScraper(config)
        self.lever = LeverScraper(config)
        self.sponsorship = SponsorshipFilter(
            blocklist=config.get("sponsorship_blocklist", []),
            enabled=config.get("sponsorship_filter_enabled", True),
        )
        self.scorer = FitScorer(config)

    async def run_scrape(self) -> list[Job]:
        """Stage 1: Scrape jobs from all ATS platforms."""
        all_jobs: list[Job] = []

        # Get company slugs from config, fall back to known lists
        ats_config = self.config.get("ats", {})
        gh_slugs = ats_config.get("greenhouse", []) or KNOWN_GREENHOUSE
        ab_slugs = ats_config.get("ashby", []) or KNOWN_ASHBY
        lv_slugs = ats_config.get("lever", []) or KNOWN_LEVER

        logger.info(f"Scraping {len(gh_slugs)} Greenhouse, {len(ab_slugs)} Ashby, {len(lv_slugs)} Lever companies")

        # Scrape each platform
        gh_jobs = await self.greenhouse.scrape_all(gh_slugs)
        all_jobs.extend(gh_jobs)
        logger.info(f"Greenhouse: {len(gh_jobs)} jobs")

        ab_jobs = await self.ashby.scrape_all(ab_slugs)
        all_jobs.extend(ab_jobs)
        logger.info(f"Ashby: {len(ab_jobs)} jobs")

        lv_jobs = await self.lever.scrape_all(lv_slugs)
        all_jobs.extend(lv_jobs)
        logger.info(f"Lever: {len(lv_jobs)} jobs")

        logger.info(f"Total scraped: {len(all_jobs)} jobs")
        return all_jobs

    async def run_full(self) -> list[Job]:
        """Run the full pipeline: scrape → filter → score → track."""
        # Stage 1: Scrape
        jobs = await self.run_scrape()
        if not jobs:
            logger.warning("No jobs found")
            return []

        # Stage 2: Sponsorship — enrich all jobs, then filter out JD-blocked ones
        jobs = self.sponsorship.enrich_jobs(jobs)
        jobs = self.sponsorship.filter_blocked(jobs)
        logger.info(f"After sponsorship: {len(jobs)} eligible jobs")

        # Stage 3: Score and rank
        jobs = self.scorer.score_jobs(jobs)
        logger.info(f"Scored {len(jobs)} jobs, top score: {jobs[0].fit_score if jobs else 0}")

        # Save to tracker
        self.tracker.upsert_jobs(jobs)
        logger.info(f"Saved {len(jobs)} jobs to tracker")

        return jobs
