"""Hirebase: queries from the profile, the monthly allowance, mapping, crawler hand-off."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from pipeline import hirebase as hb
from tests.test_autonomy import pdir  # noqa: F401

CONFIG = {"profile": {"locations": ["New York, NY", "San Francisco, CA", "Remote"]},
          "filters": {"role_keywords": ["software engineer", "backend engineer"], "locations": ["New York", "Remote"],
                      "exclude_keywords": ["senior", "staff"]}}

HIT_GH = {"_id": "a1", "job_title": "Software Engineer, Payments", "company_name": "Speechify", "company_slug": "speechify",
          "application_link": "https://job-boards.greenhouse.io/speechify/jobs/5975137004", "date_posted": "2026-11-17",
          "location_type": "Hybrid", "location_raw": "New York, NY", "visa_sponsored": True, "job_board": "greenhouse",
          "description": "Build payment rails.", "requirements_summary": "3+ years Java."}
HIT_ICIMS = {"_id": "b2", "job_title": "Backend Engineer", "company_name": "Globex", "company_slug": "globex",
             "application_link": "https://careers-globex.icims.com/jobs/123/job", "date_posted": "2026-11-16",
             "location_type": "Remote", "locations": [{"city": "Austin", "region": "Texas", "country": "United States"}],
             "visa_sponsored": False, "job_board": "iCIMS", "description": "APIs."}
HIT_SENIOR = {**HIT_ICIMS, "_id": "c3", "job_title": "Senior Backend Engineer"}


def test_key_is_private_and_validated(pdir):  # noqa: F811
    with pytest.raises(ValueError):
        hb.save_key("nope!")
    hb.save_key("hb_b32f25da-0000-4e67-80f0-000000000000")
    assert hb.get_key() == "hb_b32f25da-0000-4e67-80f0-000000000000"
    assert oct((pdir / hb.KEY_FILE).stat().st_mode)[-3:] == "600"
    assert hb.public()["has_key"] and "hb_" not in json.dumps(hb.public())


def test_queries_follow_the_profile():
    qs = hb.queries(CONFIG, dict(hb.DEFAULTS), since="2026-11-17T00:00:00Z", limit=10)
    city, remote = qs
    assert city["geo_locations"][0] == {"city": "New York", "region": "New York", "country": "United States"}
    assert city["location_types"] == ["In-Person", "Hybrid"] and remote["location_types"] == ["Remote"]
    assert city["limit"] + remote["limit"] == 10
    assert city["ingested_after"] == "2026-11-17T00:00:00Z" and city["sort_by"] == "first_ingested"
    assert city["hide_recruiting_agencies"] == "true" and "visa" not in city          # strings, not booleans
    first = hb.queries(CONFIG, {**hb.DEFAULTS, "visa_only": True}, since=None, limit=4)[0]
    assert first["days_ago"] == 2 and first["visa"] == "true"


def test_allowance_lasts_the_month():
    s = {**hb.DEFAULTS, "monthly_jobs": 480, "used": 0}
    early = hb.per_ask(s, datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert 4 <= early <= 6                                       # 480 over ~90 asks
    late = hb.per_ask({**s, "used": 470}, datetime(2026, 11, 30, 12, tzinfo=timezone.utc))
    assert late <= 10
    assert hb.per_ask({**s, "used": 480}) == 0


def test_mapping_and_filters(pdir):  # noqa: F811
    job, board = hb.to_job(HIT_GH)
    assert board == ("greenhouse", "speechify") and job.platform.value == "greenhouse"
    assert job.description.startswith("Visa sponsorship: offered") and job.posted_at.day == 17
    job2, board2 = hb.to_job(HIT_ICIMS)
    assert board2 is None and job2.platform.value == "other" and "Remote" in job2.location and job2.id == "hb-b2"


def test_fetch_counts_usage_and_remembers_where_it_left_off(pdir):  # noqa: F811
    hb.save_key("hb_" + "x" * 30)
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["x-api-key"] == "hb_" + "x" * 30
        return httpx.Response(200, json={"jobs": [HIT_GH, HIT_ICIMS, HIT_SENIOR] if "geo_locations" in body
                                         and body["location_types"] != ["Remote"] else [],
                                         "total_count": 3, "page": 1, "limit": body["limit"], "total_pages": 1})
    passes = lambda title, location, description="": "senior" not in title.lower()  # noqa: E731
    now = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
    got = hb.fetch_new(CONFIG, passes, client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)
    assert got["returned"] == 3 and [j.id for j in got["jobs"]] == ["hb-a1", "hb-b2"]
    assert got["boards"] == {("greenhouse", "speechify")}
    st = hb.state()
    assert st["used"] == 3 and st["last"]["asked_at"] == "2026-11-17T18:00:00Z"
    assert not hb.due(now=now) and hb.due(now=datetime(2026, 11, 18, 3, tzinfo=timezone.utc))
    hb.fetch_new(CONFIG, passes, client=httpx.Client(transport=httpx.MockTransport(handler)),
                 now=datetime(2026, 11, 18, 3, tzinfo=timezone.utc))
    assert seen[-1]["ingested_after"] == "2026-11-17T18:00:00Z"


def test_bad_key_is_reported_not_retried_forever(pdir):  # noqa: F811
    hb.save_key("hb_" + "y" * 30)
    got = hb.fetch_new(CONFIG, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    assert got["error"] == "Hirebase rejected the key" and hb.state()["last"]["error"]


def test_crawler_registers_boards_and_stores_the_rest(pdir, tmp_path, monkeypatch):  # noqa: F811
    import asyncio
    from types import SimpleNamespace
    from pipeline.crawler.worker import Crawler
    from pipeline.discovery.registry import BoardRegistry
    from pipeline.tracker.db import Tracker
    j_gh, b_gh = hb.to_job(HIT_GH)
    j_ic, _ = hb.to_job(HIT_ICIMS)
    monkeypatch.setattr(hb, "fetch_new", lambda config, passes, *a, **k: {
        "jobs": [j_gh, j_ic], "boards": {b_gh}, "returned": 2, "error": ""})
    tracker = Tracker(tmp_path / "t.db")
    reg = BoardRegistry(tmp_path / "r.db")
    c = Crawler(CONFIG, status_path=tmp_path / "s.json")
    c.tracker, c.registry = tracker, reg
    c._prepare = lambda jobs, known: None
    new = asyncio.run(c.poll_hirebase())
    assert new == ["hb-b2"] and tracker.get_job("hb-b2")["company"] == "Globex"
    assert tracker.get_job("hb-a1") is None                       # the crawler reads Speechify's board itself
    assert c.last_hirebase["new_boards"] == 1
    # The Simplify feed's clean-up doesn't touch Hirebase jobs
    assert "hb-b2" not in tracker.open_feed_ids()


def test_with_the_real_scraper_filters(pdir):  # noqa: F811
    """The same filter the crawler uses (title, location, description)."""
    from pipeline.orchestrator import Pipeline
    hb.save_key("hb_" + "z" * 30)
    cfg = {**CONFIG, "filters": {**CONFIG["filters"], "locations": ["New York", "Remote"]}}
    passes = next(iter(Pipeline.make_scrapers(cfg).values())).passes_filters
    body = {"jobs": [HIT_GH, HIT_ICIMS, HIT_SENIOR], "total_count": 3}
    got = hb.fetch_new(cfg, passes, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=body))))
    ids = {j.id for j in got["jobs"]}
    assert "hb-a1" in ids and "hb-b2" in ids and "hb-c3" not in ids      # senior excluded by your filters
