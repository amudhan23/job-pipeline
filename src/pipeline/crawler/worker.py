"""The crawl loop (`jobpipe watch`).

Every few seconds:
  1. take the boards that are due from the frontier (most overdue first);
  2. read each one politely (conditional GET; skip if unchanged);
  3. compare with what we hold: new postings are scored and saved with
     first_seen_at = now, vanished ones are closed, returning ones reopened;
  4. reschedule the board from its estimated posting rate (policy.py);
  5. every 5 minutes poll the job feed (304 when unchanged) — a feed hit
     makes that company's board due immediately;
  6. every few hours rediscover boards on the web;
  7. write data/crawler_status.json for the dashboard.

Postings already on a board the first time we read it are the baseline:
saved, but not announced as new.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.crawler.fetcher import Fetcher
from pipeline.crawler.policy import INITIAL_RATE, Policy
from pipeline.crawler.sources import build_sources
from pipeline.models import ApplicationStatus, Job

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
STATUS_FILE = DATA_DIR / "crawler_status.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Crawler:
    def __init__(self, config: dict[str, Any], pipeline=None, fetcher: Fetcher | None = None,
                 policy: Policy | None = None, status_path: Path | None = None,
                 on_new: Callable[[list[str]], None] | None = None,
                 after_tick: Callable[[list[str]], None] | None = None) -> None:
        from pipeline.orchestrator import Pipeline
        self.config = config
        self.cfg = config.get("crawler", {}) or {}
        self.pipeline = pipeline or Pipeline(config)
        self.tracker, self.registry = self.pipeline.tracker, self.pipeline.registry
        self.fetcher = fetcher or Fetcher(per_host=self.cfg.get("per_host", 4),
                                          min_gap=self.cfg.get("min_gap_seconds", 0.25))
        self.policy = policy or Policy(**(self.cfg.get("policy") or {}))
        self.sources = build_sources(self.pipeline.scrapers)
        self.status_path = status_path or STATUS_FILE
        self.on_new = on_new
        self.after_tick = after_tick
        self.batch = self.cfg.get("batch", 40)
        self.concurrency = asyncio.Semaphore(self.cfg.get("concurrency", 12))
        self.feed_every = self.cfg.get("feed_minutes", 5) * 60
        self.discover_every = self.cfg.get("discover_hours", 6) * 3600
        self.next_feed = utcnow()
        self.next_discovery = utcnow() + timedelta(minutes=self.cfg.get("first_discovery_minutes", 2))
        self.feed_etag: str | None = None
        self.discovery_task: asyncio.Task | None = None
        self.started = utcnow()
        self.reads: deque = deque(maxlen=5000)       # (time, platform, slug, outcome, new)
        self.new_log: deque = deque(maxlen=300)      # (time, job_id)
        self.errors: deque = deque(maxlen=30)
        self.last_discovery: dict[str, Any] = {}
        self.last_feed: dict[str, Any] = {}

    # ── One board ──────────────────────────────────────────────────────

    async def crawl_board(self, board: dict) -> list[str]:
        platform, slug = board["platform"], board["slug"]
        source = self.sources.get(platform)
        now = utcnow()
        if source is None:
            self.registry.schedule(platform, slug, next_due_at=iso(now + timedelta(hours=12)))
            return []
        async with self.concurrency:
            known = self.tracker.known_ids(platform, slug)
            listing = await source.read(self.fetcher, slug, board, known)

        last = parse_iso(board.get("last_fetch_at"))
        elapsed = (now - last).total_seconds() if last else (board.get("interval_s") or 600)
        rate = board.get("rate")
        if rate is None:
            rate = INITIAL_RATE.get(board.get("source", ""), 0.5)

        # Gone: the company left this ATS or closed its board
        if listing.status in (404, 410):
            self.registry.schedule(platform, slug, fail_count=(board.get("fail_count") or 0) + 1,
                                   last_status=listing.status, last_fetch_at=iso(now),
                                   next_due_at=iso(now + timedelta(hours=6)))
            self.reads.append((time.time(), platform, slug, "gone", 0))
            return []
        # Couldn't read it: back off without retiring it
        if listing.error and not listing.unchanged:
            streak = (board.get("error_streak") or 0) + 1
            wait = min(self.policy.max_seconds, (board.get("interval_s") or 600) * (2 ** min(streak, 5)))
            self.registry.schedule(platform, slug, error_streak=streak, last_status=listing.status,
                                   last_fetch_at=iso(now), next_due_at=iso(now + timedelta(seconds=wait)))
            self.errors.append({"at": iso(now), "board": f"{platform}/{slug}", "error": listing.error})
            self.reads.append((time.time(), platform, slug, "error", 0))
            return []

        new_ids: list[str] = []
        if not listing.unchanged:
            baseline = not board.get("bootstrapped")
            jobs = self._prepare(listing.jobs, known)
            new, closed, _ = self.tracker.observe_board(platform, slug, jobs, baseline=baseline,
                                                        close_missing=listing.complete)
            new_ids = [] if baseline else [i for i in new if self._is_open(i, jobs)]
            if closed:
                logger.info(f"{platform}/{slug}: {len(closed)} posting(s) closed")

        rate = self.policy.update_rate(rate, len(new_ids), elapsed)
        interval = self.policy.interval(rate, platform)
        interval *= random.uniform(0.9, 1.1)          # spread boards out over time
        fields = dict(next_due_at=iso(now + timedelta(seconds=interval)), interval_s=interval, rate=rate,
                      etag=listing.etag, last_modified=listing.last_modified,
                      content_hash=listing.content_hash, last_fetch_at=iso(now),
                      last_status=listing.status, bootstrapped=1, error_streak=0, fail_count=0)
        if new_ids:
            fields["last_change_at"] = iso(now)
        if not listing.unchanged:
            fields["last_job_count"] = len(listing.jobs)
        self.registry.schedule(platform, slug, **fields)
        self.registry.bump(platform, slug, fetched=True, not_modified=listing.unchanged, new=len(new_ids))
        self.reads.append((time.time(), platform, slug, "same" if listing.unchanged else "read", len(new_ids)))
        for i in new_ids:
            self.new_log.append((iso(now), i))
        if new_ids:
            logger.info(f"NEW {platform}/{slug}: {len(new_ids)} posting(s)")
        return new_ids

    def _prepare(self, jobs: list[Job], known: dict[str, str | None]) -> list[Job]:
        """Company names, then sponsorship + score for postings new to us.
        A JD that rules out sponsorship is saved as skipped, so it isn't
        announced again on every visit."""
        self.pipeline._apply_company_names(jobs)
        fresh = [j for j in jobs if j.id not in known]
        if fresh:
            sp = self.pipeline.sponsorship
            sp.enrich_jobs(fresh)
            if sp.enabled:
                for j in fresh:
                    if j.jd_blocks_sponsorship:
                        j.status = ApplicationStatus.SKIPPED
                        j.notes = "JD says no visa sponsorship"
            self.pipeline.scorer.score_jobs(fresh)
        return jobs

    @staticmethod
    def _is_open(job_id: str, jobs: list[Job]) -> bool:
        j = next((x for x in jobs if x.id == job_id), None)
        return bool(j and j.status != ApplicationStatus.SKIPPED)

    # ── Feeds ──────────────────────────────────────────────────────────

    async def poll_feeds(self) -> list[str]:
        from pipeline.discovery.feeds import SimplifyFeed
        disc = self.config.get("discovery", {})
        feed_cfg = disc.get("feeds", {})
        self.next_feed = utcnow() + timedelta(seconds=self.feed_every)
        if not feed_cfg.get("simplify_new_grad", True):
            return []
        feed = SimplifyFeed(max_age_days=disc.get("feed_max_age_days", 30),
                            categories=feed_cfg.get("simplify_categories", ["Software", "AI/ML/Data"]))
        r = await self.fetcher.get(feed.url, etag=self.feed_etag)
        self.last_feed = {"at": iso(utcnow()), "status": r.status}
        if r.not_modified or not r.ok:
            if not r.ok and not r.not_modified:
                self.errors.append({"at": iso(utcnow()), "board": "simplify feed", "error": r.error or r.status})
            return []
        self.feed_etag = {k.lower(): v for k, v in r.headers.items()}.get("etag")
        listings = r.json()
        passes = next(iter(self.pipeline.scrapers.values())).passes_filters
        jobs, boards = feed.parse(listings, passes)
        added = self.registry.add_many(boards, source="feed")
        self.registry.set_names(feed.board_names)
        # A posting from the last day on a board we crawl: read that board now
        cutoff = datetime.now() - timedelta(days=1)
        hot = {(j.platform.value, j.company_slug) for j in jobs
               if j.posted_at and j.posted_at >= cutoff and (j.platform.value, j.company_slug) in boards}
        self.registry.make_due_now(hot)
        # Jobs on boards we can't crawl (Workday, iCIMS…) come from the feed itself
        crawlable = set(self.sources)
        outside = [j for j in jobs if j.platform.value not in crawlable]
        first_time = not self.tracker.open_feed_ids()
        new_ids: list[str] = []
        if outside:
            self._prepare(outside, {})
            new_ids = self.tracker.insert_feed_jobs(outside, baseline=first_time)
            gone = self.tracker.open_feed_ids() - {j.id for j in outside}
            if gone:
                self.tracker.close_jobs(sorted(gone))
        new_ids = [] if first_time else new_ids
        self.last_feed.update(listings=len(listings), matching=len(jobs), new_boards=added,
                              boards_due_now=len(hot), new_jobs=len(new_ids))
        for i in new_ids:
            self.new_log.append((iso(utcnow()), i))
        return new_ids

    # ── Discovery ──────────────────────────────────────────────────────

    async def discover(self) -> None:
        from pipeline.scrapers.search_discover import discover as web_discover
        disc = self.config.get("discovery", {})
        started = utcnow()
        try:
            result = await web_discover(self.config, include_location=disc.get("include_location", True),
                                        recency="day", delay=disc.get("search_delay", 2.0),
                                        max_queries=disc.get("max_queries"))
            boards = {(p, s) for p, slugs in result.by_platform.items() for s in slugs}
            new = self.registry.add_many(boards, source="web")
            self.last_discovery = {"at": iso(started), "found": len(boards), "new": new,
                                   "queries": getattr(result, "queries_run", 0),
                                   "rate_limited": getattr(result, "rate_limited", False)}
            logger.info(f"Discovery: {len(boards)} boards seen, {new} new")
        except Exception as e:
            self.last_discovery = {"at": iso(started), "error": str(e)[:200]}
            logger.warning(f"Discovery failed: {e}")

    # ── Loop ───────────────────────────────────────────────────────────

    async def tick(self) -> list[str]:
        now = utcnow()
        self.pipeline._register_pins()
        new_ids: list[str] = []
        # Feed first: boards it just pointed at get read in this same pass
        if utcnow() >= self.next_feed:
            new_ids += await self._safe(self.poll_feeds(), {"platform": "feed", "slug": "simplify"})
        due = self.registry.due(iso(now), limit=self.batch)
        results = await asyncio.gather(*(self._safe(self.crawl_board(b), b) for b in due))
        new_ids += [i for r in results for i in r]
        if self.discover_every and utcnow() >= self.next_discovery and \
                (self.discovery_task is None or self.discovery_task.done()):
            self.next_discovery = utcnow() + timedelta(seconds=self.discover_every)
            if self.config.get("discovery", {}).get("enabled", True):
                self.discovery_task = asyncio.create_task(self.discover())
        if new_ids and self.on_new:
            try:
                self.on_new(new_ids)
            except Exception as e:
                logger.warning(f"on_new hook failed: {e}")
        self.write_status(len(due))
        if self.after_tick:
            try:
                self.after_tick(new_ids)
            except Exception as e:
                logger.warning(f"after-tick hook failed: {e}")
        return new_ids

    async def _safe(self, coro, board: dict):
        try:
            return await coro
        except Exception as e:
            logger.exception(f"crawl failed for {board.get('platform')}/{board.get('slug')}")
            self.errors.append({"at": iso(utcnow()), "board": f"{board.get('platform')}/{board.get('slug')}",
                                "error": f"{type(e).__name__}: {e}"[:200]})
            if board.get("platform") in self.sources:
                self.registry.schedule(board["platform"], board["slug"],
                                       next_due_at=iso(utcnow() + timedelta(minutes=30)))
            return []

    def seconds_until_next(self) -> float:
        nxt = parse_iso(self.registry.next_due() or "")
        waits = [(self.next_feed - utcnow()).total_seconds()]
        if nxt:
            waits.append((nxt - utcnow()).total_seconds())
        else:
            waits.append(0 if self.registry.due(iso(utcnow()), 1) else 30)
        return max(1.0, min(30.0, min(waits)))

    async def run(self, stop: asyncio.Event | None = None, max_ticks: int | None = None) -> None:
        stop = stop or asyncio.Event()
        ticks = 0
        logger.info("Crawler started")
        while not stop.is_set():
            await self.tick()
            ticks += 1
            if max_ticks and ticks >= max_ticks:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.seconds_until_next())
            except asyncio.TimeoutError:
                pass
        if self.discovery_task and not self.discovery_task.done():
            self.discovery_task.cancel()
        self.write_status(0, running=False)
        await self.fetcher.aclose()

    # ── Status for the dashboard ───────────────────────────────────────

    def write_status(self, due_count: int, running: bool = True) -> None:
        hour_ago = time.time() - 3600
        recent = [r for r in self.reads if r[0] >= hour_ago]
        outcomes: dict[str, int] = {}
        for r in recent:
            outcomes[r[3]] = outcomes.get(r[3], 0) + 1
        status = {
            "running": running, "pid": os.getpid(), "started_at": iso(self.started),
            "updated_at": iso(utcnow()), "due_this_tick": due_count,
            "frontier": self.registry.frontier_stats(),
            "last_hour": {"reads": len(recent), "outcomes": outcomes,
                          "new_postings": sum(r[4] for r in recent)},
            "http": dict(self.fetcher.stats),
            "next_feed_at": iso(self.next_feed), "last_feed": self.last_feed,
            "next_discovery_at": iso(self.next_discovery), "last_discovery": self.last_discovery,
            "discovering": bool(self.discovery_task and not self.discovery_task.done()),
            "recent_new": [{"at": a, "job_id": j} for a, j in list(self.new_log)[-50:]][::-1],
            "errors": list(self.errors)[-10:][::-1],
        }
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=1))
            tmp.replace(self.status_path)
        except OSError as e:
            logger.debug(f"status write failed: {e}")


def read_status(path: Path | None = None) -> dict[str, Any]:
    p = path or STATUS_FILE
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"running": False}
    upd = parse_iso(data.get("updated_at"))
    # A crawler that hasn't written in 2 minutes isn't running
    if upd and (utcnow() - upd).total_seconds() > 120:
        data["running"] = False
        data["stale"] = True
    return data
