"""Tests for run tracking, status preservation, migrations, and the dashboard API."""

import asyncio
import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer

import pytest

from pipeline.models import ApplicationStatus, ATSPlatform, Job
from pipeline.tracker.db import Tracker


def _job(jid, company="NewCo", slug="newco", score=50.0):
    j = Job(id=jid, title="Software Engineer", company=company, company_slug=slug,
            location="New York", url=f"https://jobs.ashbyhq.com/{slug}/{jid}",
            platform=ATSPlatform.ASHBY, description="jd",
            posted_at=datetime.now(timezone.utc))
    j.fit_score = score
    return j


@pytest.fixture
def tracker(tmp_path):
    return Tracker(tmp_path / "tracker.db")


def test_rerun_keeps_applied_status(tracker):
    tracker.upsert_jobs([_job("1", score=60)])
    tracker.update_status("1", ApplicationStatus.APPLIED, notes="sent via referral")
    tracker.upsert_jobs([_job("1", score=75)])          # next pipeline run
    row = tracker.get_job("1")
    assert row["status"] == "applied"
    assert row["notes"] == "sent via referral"
    assert row["applied_at"] is not None
    assert row["fit_score"] == 75                        # scraped fields still refresh


def test_runs_scope_jobs(tracker):
    r1 = tracker.start_run(None)
    tracker.upsert_jobs([_job("old1"), _job("both")], run_id=r1)
    r2 = tracker.start_run(7)
    tracker.upsert_jobs([_job("both"), _job("new1")], run_id=r2)
    tracker.finish_run(r2, 2)

    assert tracker.latest_run()["id"] == r2
    assert tracker.latest_run()["max_age_days"] == 7
    assert {j["id"] for j in tracker.get_jobs(r2)} == {"both", "new1"}
    assert {j["id"] for j in tracker.get_jobs(None)} == {"old1", "both", "new1"}
    assert tracker.get_stats(r2)["total"] == 2


def test_migrates_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, "
                 "company_slug TEXT, location TEXT, url TEXT, platform TEXT, description TEXT, "
                 "department TEXT, posted_at TEXT, scraped_at TEXT, sponsorship TEXT, "
                 "h1b_filings_count INTEGER, fit_score REAL, fit_breakdown TEXT, resume_path TEXT, "
                 "status TEXT, applied_at TEXT, notes TEXT)")
    conn.execute("INSERT INTO jobs (id, title, company, status) VALUES ('legacy', 't', 'Amazon', 'scored')")
    conn.commit()
    conn.close()

    t = Tracker(path)
    assert t.latest_run() is None
    assert [j["id"] for j in t.get_jobs(None)] == ["legacy"]   # legacy rows kept
    run = t.start_run(7)
    t.upsert_jobs([_job("fresh")], run_id=run)
    assert [j["id"] for j in t.get_jobs(run)] == ["fresh"]     # ...but not in latest run


def test_registry_migrates_and_names(tmp_path):
    from pipeline.discovery import BoardRegistry
    path = tmp_path / "reg.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE boards (platform TEXT NOT NULL, slug TEXT NOT NULL, source TEXT NOT NULL, "
                 "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_job_count INTEGER DEFAULT 0, "
                 "fail_count INTEGER DEFAULT 0, PRIMARY KEY (platform, slug))")
    conn.execute("INSERT INTO boards VALUES ('ashby','kalshi','feed','2026-11-13','2026-11-13',8,0)")
    conn.commit()
    conn.close()

    reg = BoardRegistry(path)
    (board,) = reg.all()
    assert board.display_name == "Kalshi"            # prettified until a name is known
    reg.set_names({("ashby", "kalshi"): "Kalshi Inc"})
    assert reg.names() == {("ashby", "kalshi"): "Kalshi Inc"}
    assert reg.all()[0].display_name == "Kalshi Inc"


def test_run_full_records_run_and_names(tmp_path):
    from pipeline import orchestrator
    from pipeline.discovery import BoardRegistry
    from tests.test_discovery import FakeScraper

    registry = BoardRegistry(tmp_path / "r.db")
    registry.add("ashby", "ande", "feed")
    registry.add("ashby", "humble-robotics", "web")
    registry.set_names({("ashby", "ande"): "Ande"})

    pipe = orchestrator.Pipeline({"filters": {}, "ats": {}}, registry=registry)
    pipe.tracker = Tracker(tmp_path / "t.db")
    pipe.scrapers = {"ashby": FakeScraper({
        "ande": [_job("a", company="ande", slug="ande")],
        "humble-robotics": [_job("h", company="humble-robotics", slug="humble-robotics")],
    })}

    jobs = asyncio.run(pipe.run_full(discover=False))
    assert {j.company for j in jobs} == {"Ande", "Humble Robotics"}
    run = pipe.tracker.latest_run()
    assert run["job_count"] == 2
    assert {j["id"] for j in pipe.tracker.get_jobs(run["id"])} == {"a", "h"}


def test_empty_run_still_recorded(tmp_path):
    from pipeline import orchestrator
    from pipeline.discovery import BoardRegistry

    pipe = orchestrator.Pipeline({"filters": {}, "ats": {}},
                                 registry=BoardRegistry(tmp_path / "r.db"))
    pipe.tracker = Tracker(tmp_path / "t.db")
    pipe.tracker.upsert_jobs([_job("stale")], run_id=pipe.tracker.start_run(None))
    asyncio.run(pipe.run_full(discover=False))
    run = pipe.tracker.latest_run()
    assert run["job_count"] == 0
    assert pipe.tracker.get_jobs(run["id"]) == []      # dashboard won't show "stale"


def test_dashboard_api_defaults_to_latest_run(tmp_path):
    from pipeline.dashboard.app import DashboardHandler

    t = Tracker(tmp_path / "t.db")
    t.upsert_jobs([_job("amazon-old", company="Amazon")], run_id=t.start_run(None))
    latest = t.start_run(7)
    t.upsert_jobs([_job("kalshi-new", company="Kalshi")], run_id=latest)
    t.finish_run(latest, 1)

    DashboardHandler.db_path = tmp_path / "t.db"
    srv = HTTPServer(("127.0.0.1", 0), DashboardHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        get = lambda q: json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs{q}"))  # noqa: E731
        latest_view = get("")
        assert [j["company"] for j in latest_view["jobs"]] == ["Kalshi"]
        assert latest_view["scope"] == "latest" and latest_view["saved_total"] == 2
        assert latest_view["run"]["max_age_days"] == 7
        assert {j["company"] for j in get("?scope=all")["jobs"]} == {"Amazon", "Kalshi"}
    finally:
        srv.shutdown()
