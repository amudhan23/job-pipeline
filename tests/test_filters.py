"""Tests for title/location filtering and job freshness."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.models import ATSPlatform, Job
from pipeline.scrapers.base import BaseScraper, _has_word


class Scraper(BaseScraper):
    async def scrape_company(self, company_slug):
        return []


FILTERS = {
    "filters": {
        "role_keywords": ["software engineer"],
        "exclude_keywords": ["senior", "sr", "intern", "internship"],
        "locations": ["New York", "San Francisco", "Remote"],
        "exclude_locations": ["canada", "india", "bangalore", "uk", "ontario"],
    }
}


@pytest.fixture
def s():
    return Scraper(FILTERS)


class TestTitleFilter:
    def test_sr_abbreviation_excluded(self, s):
        assert not s.passes_filters("Sr Software Engineer - Veza", "Remote")
        assert not s.passes_filters("Sr. Software Engineer", "New York, NY")

    def test_internal_tools_not_mistaken_for_intern(self, s):
        assert s.passes_filters("Software Engineer, Internal Tools", "New York, NY")
        assert s.passes_filters("Software Engineer, International Payments", "Remote")

    def test_intern_still_excluded(self, s):
        assert not s.passes_filters("Software Engineer Intern", "New York, NY")
        assert not s.passes_filters("Software Engineer Internship 2027", "New York, NY")

    def test_has_word_boundaries(self):
        assert _has_word("sr. engineer", "sr")
        assert not _has_word("israel r&d", "sr")
        assert _has_word("co-op software engineer", "co-op")


class TestLocationFilter:
    def test_remote_canada_rejected(self, s):
        assert not s.passes_filters("Software Engineer", "Remote Canada")
        assert not s.passes_filters("Software Engineer", "Remote - Ontario")

    def test_remote_us_and_canada_kept(self, s):
        assert s.passes_filters("Software Engineer", "Remote in USA; Remote in Canada")

    def test_target_city_plus_foreign_kept(self, s):
        assert s.passes_filters("Software Engineer", "Toronto, Canada; New York, NY")

    def test_foreign_office_rejected_even_if_jd_says_remote(self, s):
        assert not s.passes_filters("Software Engineer", "Bangalore, India",
                                    description="This role is remote-friendly")

    def test_workday_hyphenated_location(self, s):
        assert s.passes_filters("Software Engineer", "US-NY-New-York")

    def test_plain_remote_kept(self, s):
        assert s.passes_filters("Software Engineer", "Remote")

    def test_other_us_city_rejected(self, s):
        assert not s.passes_filters("Software Engineer", "Austin, TX")

    def test_no_location_uses_jd(self, s):
        assert s.passes_filters("Software Engineer", "", description="Fully remote role")
        assert not s.passes_filters("Software Engineer", "", description="Onsite in Austin")


# ── Freshness ──────────────────────────────────────────────────────────

def _job(jid, posted_at):
    return Job(id=jid, title="Software Engineer", company="c", company_slug="c",
               location="New York", url=f"https://jobs.ashbyhq.com/c/{jid}",
               platform=ATSPlatform.ASHBY, description="jd", posted_at=posted_at)


def test_job_age_handles_naive_and_aware():
    from pipeline.orchestrator import job_age
    aware = _job("a", datetime.now(timezone.utc) - timedelta(hours=5))
    naive = _job("n", datetime.now() - timedelta(hours=5))
    assert timedelta(hours=4) < job_age(aware) < timedelta(hours=6)
    assert timedelta(hours=4) < job_age(naive) < timedelta(hours=6)
    assert job_age(_job("u", None)) is None


def test_recency_day_keeps_only_fresh_jobs(tmp_path, monkeypatch):
    from pipeline import orchestrator
    from pipeline.discovery import BoardRegistry
    from tests.test_discovery import FakeScraper

    now = datetime.now(timezone.utc)
    fresh = _job("fresh", now - timedelta(hours=3))
    old = _job("old", now - timedelta(days=3))
    undated = _job("undated", None)

    registry = BoardRegistry(tmp_path / "r.db")
    registry.add("ashby", "c", "web")
    pipe = orchestrator.Pipeline({"filters": {}, "ats": {}}, registry=registry)
    pipe.scrapers = {"ashby": FakeScraper({"c": [fresh, old, undated]})}

    jobs = asyncio.run(pipe.run_scrape(discover=False, recency="day"))
    assert [j.id for j in jobs] == ["fresh"]
    assert pipe.stats.too_old_dropped == 1 and pipe.stats.undated_dropped == 1

    # No recency → nothing dropped
    jobs = asyncio.run(pipe.run_scrape(discover=False))
    assert len(jobs) == 3

    # Explicit days override
    jobs = asyncio.run(pipe.run_scrape(discover=False, max_age_days=5))
    assert {j.id for j in jobs} == {"fresh", "old"}
