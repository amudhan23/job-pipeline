"""Real-time crawler — against a fake ATS (httpx MockTransport), no network."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pipeline.crawler.fetcher import Fetcher
from pipeline.crawler.policy import Policy
from pipeline.crawler.worker import Crawler, iso, read_status, utcnow
from pipeline.discovery.registry import BoardRegistry
from pipeline.tracker.db import Tracker

CONFIG = {
    "filters": {"role_keywords": ["software engineer"], "exclude_keywords": ["senior", "intern"],
                "locations": ["New York", "Remote"], "exclude_locations": ["India"]},
    "discovery": {"enabled": False, "feeds": {"simplify_new_grad": False}},
    "sponsorship_filter_enabled": True,
    "crawler": {"feed_minutes": 5, "discover_hours": 0},
}


class FakeATS:
    """Greenhouse (light list + detail), Lever and Ashby boards whose
    postings the test changes between ticks. Honors If-None-Match."""

    def __init__(self):
        self.gh = {"acme": [self._gh(1, "Software Engineer", "New York, NY")]}
        self.lever = {"leverco": [self._lv("a1", "Software Engineer II", "Remote - US")]}
        self.requests: list[str] = []
        self.gone = set()

    @staticmethod
    def _gh(i, title, loc, content="<p>Build Java services.</p>"):
        return {"id": i, "title": title, "location": {"name": loc},
                "updated_at": "2026-11-13T10:00:00-05:00", "first_published": "2026-11-13T10:00:00-05:00",
                "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{i}",
                "company_name": "Acme", "_content": content}

    @staticmethod
    def _lv(i, title, loc):
        return {"id": i, "text": title, "categories": {"location": loc, "team": "Eng"},
                "descriptionPlain": "Python and Go", "hostedUrl": f"https://jobs.lever.co/leverco/{i}",
                "createdAt": 1763046000000}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        path = request.url.path
        if path.startswith("/v1/boards/"):
            parts = path.split("/")
            slug = parts[3]
            if slug in self.gone:
                return httpx.Response(404)
            jobs = self.gh.get(slug, [])
            if len(parts) == 6:                      # detail
                job = next(j for j in jobs if str(j["id"]) == parts[5])
                return httpx.Response(200, json={**{k: v for k, v in job.items() if k != "_content"},
                                                  "content": job["_content"]})
            body = json.dumps({"jobs": [{k: v for k, v in j.items() if k != "_content"} for j in jobs]})
            etag = f'"{hash(body)}"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            return httpx.Response(200, content=body, headers={"ETag": etag, "Content-Type": "application/json"})
        if path.startswith("/v0/postings/"):
            slug = path.split("/")[3]
            return httpx.Response(200, json=self.lever.get(slug, []))
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    from pipeline.orchestrator import Pipeline
    ats = FakeATS()
    registry = BoardRegistry(tmp_path / "registry.db")
    pipe = Pipeline(CONFIG, registry=registry)
    pipe.tracker = Tracker(tmp_path / "tracker.db")
    registry.add("greenhouse", "acme", "web")
    registry.add("lever", "leverco", "feed")
    fetcher = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(ats.handler)), min_gap=0)
    seen = []
    crawler = Crawler(CONFIG, pipeline=pipe, fetcher=fetcher, status_path=tmp_path / "status.json",
                      on_new=seen.extend)
    return crawler, ats, seen, tmp_path


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _force_due(crawler):
    with crawler.registry._conn() as c:
        c.execute("UPDATE boards SET next_due_at = NULL")


def test_first_read_is_baseline_then_new_postings_are_announced(env):
    crawler, ats, seen, _ = env

    async def scenario():
        first = await crawler.tick()
        assert first == []                                   # baseline: nothing "new"
        t = crawler.tracker
        job = t.get_job("gh-acme-1")
        assert job["baseline"] == 1 and "Java" in job["description"] and job["fit_score"] > 0
        assert t.get_job("lv-leverco-a1")["baseline"] == 1

        # A posting goes live on Acme's board (and one that doesn't fit)
        ats.gh["acme"].append(ats._gh(2, "Software Engineer, Payments", "Remote"))
        ats.gh["acme"].append(ats._gh(3, "Senior Software Engineer", "New York"))
        _force_due(crawler)
        new = await crawler.tick()
        assert new == ["gh-acme-2"] and seen == ["gh-acme-2"]
        j2 = t.get_job("gh-acme-2")
        assert j2["baseline"] == 0 and j2["first_seen_at"] and j2["closed_at"] is None
        assert "Java" in j2["description"]                  # detail fetched for the new one only
        details = [u for u in ats.requests if "/jobs/" in u]
        assert details.count("https://boards-api.greenhouse.io/v1/boards/acme/jobs/2") == 1
        assert not any(u.endswith("/jobs/3") for u in details)   # filtered on title before any detail read
        ev = t.events()
        assert ev[0]["kind"] == "new" and ev[0]["job_id"] == "gh-acme-2"
        await crawler.fetcher.aclose()

    run(scenario())


def test_unchanged_board_is_a_304_and_vanished_posting_closes(env):
    crawler, ats, _, _ = env

    async def scenario():
        await crawler.tick()
        _force_due(crawler)
        await crawler.tick()
        board = next(b for b in crawler.registry.due("9999", 10) if b["slug"] == "acme")
        assert board["not_modified"] == 1 and board["fetches"] == 2   # second read was a 304
        # The posting is taken down
        ats.gh["acme"] = []
        _force_due(crawler)
        await crawler.tick()
        job = crawler.tracker.get_job("gh-acme-1")
        assert job["closed_at"] is not None
        assert crawler.tracker.events()[0]["kind"] == "closed"
        assert "gh-acme-1" not in {j["id"] for j in crawler.tracker.open_jobs()}
        await crawler.fetcher.aclose()

    run(scenario())


def test_board_gone_is_retired_after_repeats_and_errors_back_off(env):
    crawler, ats, _, _ = env

    async def scenario():
        await crawler.tick()
        ats.gone.add("acme")
        for _ in range(3):
            _force_due(crawler)
            await crawler.tick()
        assert "acme" not in {b.slug for b in crawler.registry.active("greenhouse")}
        await crawler.fetcher.aclose()

    run(scenario())


def test_schedule_follows_posting_rate(env):
    crawler, ats, _, _ = env

    async def scenario():
        await crawler.tick()
        rows = {b["slug"]: b for b in crawler.registry.due("9999", 10)}
        # A board a feed pointed us to starts with a higher rate → sooner revisit
        assert rows["leverco"]["rate"] > rows["acme"]["rate"]
        assert rows["leverco"]["interval_s"] < rows["acme"]["interval_s"]
        nxt = datetime.strptime(rows["acme"]["next_due_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert timedelta(minutes=1) < nxt - utcnow() < timedelta(hours=3)
        await crawler.fetcher.aclose()

    run(scenario())


def test_no_sponsorship_jd_saved_as_skipped_not_announced(env):
    crawler, ats, seen, _ = env

    async def scenario():
        await crawler.tick()
        ats.gh["acme"].append(ats._gh(4, "Software Engineer", "New York",
                                      content="We are unable to sponsor visas for this role."))
        _force_due(crawler)
        new = await crawler.tick()
        assert new == [] and seen == []
        job = crawler.tracker.get_job("gh-acme-4")
        assert job["status"] == "skipped" and "sponsorship" in job["notes"]
        _force_due(crawler)
        assert await crawler.tick() == []                   # not re-announced
        await crawler.fetcher.aclose()

    run(scenario())


def test_status_file_for_dashboard(env):
    crawler, _, _, tmp = env

    async def scenario():
        await crawler.tick()
        st = read_status(tmp / "status.json")
        assert st["running"] and st["frontier"]["boards"] == 2 and st["last_hour"]["reads"] == 2
        await crawler.fetcher.aclose()

    run(scenario())


def test_policy_shapes():
    p = Policy()
    busy, quiet = p.interval(10), p.interval(0)
    assert busy < p.interval(1) < quiet <= p.max_seconds
    assert p.interval(50, platform="workday") >= p.slow_min_seconds
    # nothing new for a long gap pulls the rate down; a new posting pushes it up
    assert p.update_rate(1.0, 0, 86400) < 1.0 < p.update_rate(1.0, 3, 3600)


def test_fetcher_backs_off_on_429():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        f = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap=0.01)
        r = await f.get("https://api.example.com/x")
        assert r.ok and len(calls) == 2 and f.stats["throttled"] == 1
        assert f.hosts["api.example.com"].gap > 0.01            # the host's gap widened
        await f.aclose()

    run(scenario())
