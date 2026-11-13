"""Tests for web-first discovery: registry, feeds, merge, and the full run."""

import asyncio
import time

import pytest

from pipeline.discovery import BoardRegistry, SimplifyFeed, normalize_url
from pipeline.discovery.registry import MAX_FAILURES
from pipeline.models import ATSPlatform, Job
from pipeline.scrapers import search_discover
from pipeline.scrapers.search_discover import DiscoveryResult, RateLimited, generate_queries


CONFIG = {
    "filters": {
        "role_keywords": ["software engineer", "backend engineer"],
        "exclude_keywords": ["senior"],
        "locations": ["New York", "Remote"],
    },
    "discovery": {"search_delay": 0, "feeds": {"simplify_new_grad": True,
                                               "simplify_categories": None}},
    "ats": {},
}


@pytest.fixture
def registry(tmp_path):
    return BoardRegistry(tmp_path / "registry.db")


# ── Registry ────────────────────────────────────────────────────────────

class TestRegistry:
    def test_add_reports_new_then_known(self, registry):
        assert registry.add("greenhouse", "stripe", "web") is True
        assert registry.add("greenhouse", "stripe", "feed") is False
        assert registry.slugs("greenhouse") == ["stripe"]

    def test_add_many_counts_new(self, registry):
        registry.add("ashby", "openai", "web")
        new = registry.add_many({("ashby", "openai"), ("ashby", "ramp")}, "feed")
        assert new == 1

    def test_dead_board_retires_after_repeated_failures(self, registry):
        registry.add("lever", "netflix", "web")
        for _ in range(MAX_FAILURES):
            registry.record_result("lever", "netflix", None)
        assert registry.slugs("lever") == []

    def test_success_resets_failures(self, registry):
        registry.add("lever", "spotify", "web")
        registry.record_result("lever", "spotify", None)
        registry.record_result("lever", "spotify", 4)
        (board,) = registry.active("lever")
        assert board.fail_count == 0 and board.last_job_count == 4


# ── URL normalization ──────────────────────────────────────────────────

def test_normalize_url_strips_tracking_and_case():
    a = normalize_url("https://Jobs.Lever.co/spotify/abc/?utm_source=Simplify&ref=Simplify")
    b = normalize_url("https://jobs.lever.co/spotify/abc")
    assert a == b


# ── Simplify feed ──────────────────────────────────────────────────────

def _listing(**kw):
    base = {
        "id": "x1", "company_name": "NewCo", "title": "Software Engineer",
        "url": "https://job-boards.greenhouse.io/newco/jobs/1",
        "locations": ["New York, NY"], "active": True, "is_visible": True,
        "date_posted": int(time.time()) - 86400, "category": "Software",
        "sponsorship": "Other",
    }
    base.update(kw)
    return base


def _accept_all(title, location):
    return True


class TestSimplifyFeed:
    def test_registers_board_from_job_url(self):
        jobs, boards = SimplifyFeed().parse([_listing()], _accept_all)
        assert boards == {("greenhouse", "newco")}
        assert jobs[0].platform == ATSPlatform.GREENHOUSE
        assert jobs[0].company_slug == "newco"

    def test_skips_inactive_and_stale(self):
        stale = _listing(id="s", date_posted=int(time.time()) - 90 * 86400)
        closed = _listing(id="c", active=False)
        jobs, _ = SimplifyFeed(max_age_days=30).parse([stale, closed], _accept_all)
        assert jobs == []

    def test_category_filter(self):
        hw = _listing(category="Hardware")
        jobs, _ = SimplifyFeed(categories=["Software"]).parse([hw], _accept_all)
        assert jobs == []

    def test_no_sponsorship_label_blocks(self):
        jobs, _ = SimplifyFeed().parse(
            [_listing(sponsorship="Does Not Offer Sponsorship")], _accept_all)
        assert jobs[0].jd_blocks_sponsorship is True

    def test_board_registered_even_when_role_filtered_out(self):
        reject_all = lambda t, l: False  # noqa: E731
        jobs, boards = SimplifyFeed().parse([_listing()], reject_all)
        assert jobs == [] and boards == {("greenhouse", "newco")}

    def test_unscrapable_ats_link_registers_no_board(self):
        jv = _listing(url="https://jobs.jobvite.com/acme/job/oABC")
        jobs, boards = SimplifyFeed().parse([jv], _accept_all)
        assert boards == set() and jobs[0].platform == ATSPlatform.OTHER

    def test_workday_roles_register_their_board(self):
        wd = _listing(url="https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123")
        jobs, boards = SimplifyFeed().parse([wd], _accept_all)
        assert jobs[0].platform == ATSPlatform.WORKDAY
        assert boards == {("workday", "acme.wd1.myworkdayjobs.com/careers")}


# ── Query generation for discovery ─────────────────────────────────────

def test_per_keyword_queries_cover_each_role():
    qs = generate_queries(CONFIG, platforms=["ashby"], per_keyword=True)
    assert len(qs) == 2
    assert '"software engineer"' in qs[0][1] and '"backend engineer"' in qs[1][1]


def test_scrapable_only_skips_login_platforms():
    qs = generate_queries(CONFIG, scrapable_only=True)
    assert "jobvite" not in {p for p, _ in qs}


def test_discovery_stops_cleanly_when_rate_limited(monkeypatch):
    calls = []

    async def fake_search(query, key, recency):
        calls.append(query)
        if len(calls) > 1:
            raise RateLimited("duckduckgo")
        return ["https://jobs.ashbyhq.com/newco/123"]

    monkeypatch.setattr(search_discover, "_search_duckduckgo", fake_search)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    result = asyncio.run(search_discover.discover(CONFIG, delay=0))
    assert result.by_platform == {"ashby": {"newco"}}
    assert len(calls) == 2  # stopped right after the throttle
    assert result.rate_limited and result.queries_run == 1


def test_discovery_diagnostics_count_links(monkeypatch):
    async def fake_search(query, key, recency):
        return ["https://example.com/blog", "https://jobs.lever.co/acme/1",
                "https://jobs.jobvite.com/acme/job/x"]  # jobvite: not scrapable

    monkeypatch.setattr(search_discover, "_search_duckduckgo", fake_search)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    r = asyncio.run(search_discover.discover(CONFIG, delay=0, max_queries=1))
    assert (r.queries_run, r.result_links, r.ats_links) == (1, 3, 1)
    assert not r.rate_limited


class TestDDGParser:
    def test_href_before_class_still_parsed(self):
        body = '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjobs.lever.co%2Facme%2F1" class="result__a">x</a>'
        assert search_discover.parse_ddg_html(body) == ["https://jobs.lever.co/acme/1"]

    def test_lite_page_direct_links(self):
        body = ("<a rel='nofollow' href='https://boards.greenhouse.io/newco/jobs/9' "
                "class='result-link'>NewCo</a>")
        assert search_discover.parse_ddg_html(body) == ["https://boards.greenhouse.io/newco/jobs/9"]

    def test_skips_duckduckgo_own_links_and_dupes(self):
        body = ('<a href="https://duckduckgo.com/settings">s</a>'
                '<a href="https://jobs.ashbyhq.com/a/1">1</a>'
                '<a href="https://jobs.ashbyhq.com/a/1">dup</a>')
        assert search_discover.parse_ddg_html(body) == ["https://jobs.ashbyhq.com/a/1"]


# ── Full run: a company in no config gets found and scraped ────────────

def _job(slug, title, url, platform=ATSPlatform.ASHBY, description="JD text"):
    return Job(id=f"{slug}-{title}", title=title, company=slug, company_slug=slug,
               location="New York, NY", url=url, platform=platform,
               description=description)


class FakeScraper:
    def __init__(self, boards):
        self.boards = boards  # slug -> list[Job] or None (fails)

    def passes_filters(self, title, location, description=""):
        return True

    async def scrape_many(self, slugs, concurrency=8):
        jobs, status = [], {}
        for s in slugs:
            result = self.boards.get(s)
            status[s] = None if result is None else len(result)
            jobs.extend(result or [])
        return jobs, status


def test_run_discovers_and_scrapes_unknown_company(monkeypatch, registry):
    from pipeline import orchestrator

    async def fake_web(config, **kw):
        r = DiscoveryResult()
        r.add("ashby", "brandnewco")          # posted its first job this week
        r.add("ashby", "deadco")              # board 404s
        return r

    feed_listing = _listing(
        id="f1", company_name="FeedCo", title="Software Engineer",
        url="https://acme.wd1.myworkdayjobs.com/job/1",  # Workday — feed-only
    )
    dup_listing = _listing(  # same posting the board scrape will return
        id="f2", company_name="brandnewco", title="Backend Engineer",
        url="https://jobs.ashbyhq.com/brandnewco/abc?utm_source=Simplify",
    )

    async def fake_fetch(self):
        return [feed_listing, dup_listing]

    monkeypatch.setattr(orchestrator, "web_discover", fake_web)
    monkeypatch.setattr(SimplifyFeed, "fetch", fake_fetch)

    pipe = orchestrator.Pipeline(CONFIG, registry=registry)
    board_job = _job("brandnewco", "Backend Engineer", "https://jobs.ashbyhq.com/brandnewco/abc")
    fake = FakeScraper({"brandnewco": [board_job], "deadco": None})
    pipe.scrapers = {"ashby": fake, "greenhouse": fake}

    jobs = asyncio.run(pipe.run_scrape())

    titles = {(j.company, j.title) for j in jobs}
    # found via web, never configured (name comes from the feed listing, verbatim)
    assert ("brandnewco", "Backend Engineer") in titles
    assert ("FeedCo", "Software Engineer") in titles       # Workday role via feed, name intact
    assert len(jobs) == 2                                  # feed duplicate dropped
    kept = next(j for j in jobs if j.company_slug == "brandnewco")
    assert kept.description == "JD text"                   # board version (has JD) wins
    assert pipe.stats.new_boards["web search"] == 2
    assert pipe.stats.boards_failed == 1
    assert "brandnewco" in registry.slugs("ashby")



def test_application_url_matches_posting():
    assert normalize_url("https://jobs.ashbyhq.com/cursor/u-1/application?embed=true") == \
        normalize_url("https://jobs.ashbyhq.com/cursor/u-1")
    assert normalize_url("https://jobs.lever.co/spotify/abc/apply") == normalize_url("https://jobs.lever.co/spotify/abc")


def test_stale_feed_job_dropped_when_board_lacks_it(monkeypatch, registry):
    """Anysphere bug: the feed listed a job the company had closed. We scraped
    the company's board this run and the job wasn't there → drop it."""
    from pipeline import orchestrator

    async def no_web(config, **kw):
        return DiscoveryResult()

    listings = [
        _listing(id="gone", company_name="Anysphere", title="Software Engineer New Grad",
                 url="https://jobs.ashbyhq.com/cursor/u-closed/application?embed=true"),
        _listing(id="live", company_name="Anysphere", title="Software Engineer",
                 url="https://jobs.ashbyhq.com/cursor/u-live/application"),
        _listing(id="wd", company_name="BigCo", title="Software Engineer",
                 url="https://bigco.wd1.myworkdayjobs.com/External/job/NY/SWE_R1"),
    ]

    async def fake_fetch(self):
        return listings

    monkeypatch.setattr(orchestrator, "web_discover", no_web)
    monkeypatch.setattr(SimplifyFeed, "fetch", fake_fetch)
    pipe = orchestrator.Pipeline(CONFIG, registry=registry)
    live = _job("cursor", "Software Engineer", "https://jobs.ashbyhq.com/cursor/u-live")
    pipe.scrapers = {"ashby": FakeScraper({"cursor": [live]}),
                     "workday": FakeScraper({"bigco.wd1.myworkdayjobs.com/External": []})}

    jobs = asyncio.run(pipe.run_scrape())
    urls = sorted(j.url for j in jobs)
    assert "https://jobs.ashbyhq.com/cursor/u-live" in urls
    assert not any("u-closed" in u for u in urls)           # closed → dropped
    assert any("myworkdayjobs" in u for u in urls)          # Workday absence isn't proof → kept
    assert pipe.stats.closed_dropped == 1
