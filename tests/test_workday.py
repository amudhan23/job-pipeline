"""Tests for the Workday scraper (public CXS API)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pipeline.models import ATSPlatform
from pipeline.scrapers import workday
from pipeline.scrapers.search_discover import _extract_slug
from pipeline.scrapers.workday import (
    WorkdayScraper, board_slug_from_url, parse_board, parse_posted_on,
)


class TestBoardIdentity:
    def test_plain_url(self):
        url = "https://analogdevices.wd1.myworkdayjobs.com/External/job/US-MA-Wilmington/Eng_R266131"
        assert board_slug_from_url(url) == "analogdevices.wd1.myworkdayjobs.com/External"

    def test_locale_segment_any_case(self):
        a = "https://generalmotors.wd5.myworkdayjobs.com/en-CA/Careers_GM/job/Austin/SWE_JR-1"
        b = "https://acme.wd1.myworkdayjobs.com/en-us/careers/job/x"
        assert board_slug_from_url(a) == "generalmotors.wd5.myworkdayjobs.com/Careers_GM"
        assert board_slug_from_url(b) == "acme.wd1.myworkdayjobs.com/careers"

    def test_myworkdaysite(self):
        url = "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/TX/Eng_R4032-26"
        assert board_slug_from_url(url) == "wd5.myworkdaysite.com/microchiphr/External"
        board = parse_board("wd5.myworkdaysite.com/microchiphr/External")
        assert board.api_base == "https://wd5.myworkdaysite.com/wday/cxs/microchiphr/External"
        assert board.public_url("/job/x") == "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/x"

    def test_url_without_site_has_no_board(self):
        assert board_slug_from_url("https://acme.wd1.myworkdayjobs.com/job/1") is None

    def test_discovery_extracts_workday(self):
        url = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/X_JR1"
        assert _extract_slug(url) == ("workday", "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")

    @pytest.mark.parametrize("bad", [
        "evil.com/x",
        "acme.wd1.myworkdayjobs.com.evil.com/site",
        "acme.wd1.myworkdayjobs.com/../admin",
        "acme@evil.com.wd1.myworkdayjobs.com/site",
        "acme.wd1.myworkdayjobs.com/site/extra",
    ])
    def test_unsafe_slugs_rejected(self, bad):
        assert parse_board(bad) is None


class TestPostedOn:
    NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)

    @pytest.mark.parametrize("text,days", [
        ("Posted Today", 0), ("Posted Yesterday", 1),
        ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 31),
    ])
    def test_labels(self, text, days):
        assert parse_posted_on(text, self.NOW) == self.NOW - timedelta(days=days)

    def test_unknown(self):
        assert parse_posted_on("", self.NOW) is None
        assert parse_posted_on("Recently", self.NOW) is None


# ── Full scrape against a fake Workday server ──────────────────────────

CONFIG = {"filters": {
    "role_keywords": ["software engineer"],
    "exclude_keywords": ["senior"],
    "locations": ["New York", "Remote"],
    "exclude_locations": ["canada"],
}}

LIST = [
    {"title": "Software Engineer I", "externalPath": "/job/US-NY-New-York/SWE-I_R1",
     "locationsText": "US-NY-New-York", "postedOn": "Posted 2 Days Ago", "bulletFields": ["R1"]},
    {"title": "Software Engineer II", "externalPath": "/job/x/SWE-II_R2",
     "locationsText": "3 Locations", "postedOn": "Posted Today", "bulletFields": ["R2"]},
    {"title": "Software Engineer", "externalPath": "/job/US-AZ-Chandler/SWE_R3",
     "locationsText": "US-AZ-Chandler", "postedOn": "Posted Today", "bulletFields": ["R3"]},
    {"title": "Senior Software Engineer", "externalPath": "/job/US-NY/Sr_R4",
     "locationsText": "US-NY-New-York", "postedOn": "Posted Today", "bulletFields": ["R4"]},
]

DETAILS = {
    "/job/US-NY-New-York/SWE-I_R1": {
        "title": "Software Engineer I", "jobDescription": "<p>Java</p>",
        "location": "US-NY-New-York", "startDate": "2026-11-11", "jobReqId": "R1",
        "externalUrl": "https://acme.wd1.myworkdayjobs.com/External/job/US-NY-New-York/SWE-I_R1",
    },
    "/job/x/SWE-II_R2": {
        "title": "Software Engineer II", "jobDescription": "<p>Rust</p>",
        "location": "Toronto, Canada", "additionalLocations": ["New York, NY", "Austin, TX"],
        "postedOn": "Posted Today", "jobReqId": "R2",
    },
}


@pytest.fixture
def fake_workday(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        base = "/wday/cxs/acme/External"
        if request.method == "POST" and request.url.path == f"{base}/jobs":
            body = json.loads(request.content)
            page = LIST[body["offset"]:body["offset"] + body["limit"]]
            # Like Workday: "total" only on the first page
            return httpx.Response(200, json={"total": len(LIST) if body["offset"] == 0 else 0,
                                             "jobPostings": page})
        path = request.url.path.removeprefix(base)
        if request.method == "GET" and path in DETAILS:
            return httpx.Response(200, json={"jobPostingInfo": DETAILS[path],
                                             "hiringOrganization": {"name": "Acme Corp"}})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(workday.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return calls


def test_scrape_board(fake_workday):
    jobs = asyncio.run(WorkdayScraper(CONFIG).scrape_company("acme.wd1.myworkdayjobs.com/External"))
    by_title = {j.title: j for j in jobs}
    assert set(by_title) == {"Software Engineer I", "Software Engineer II"}

    one = by_title["Software Engineer I"]
    assert one.platform == ATSPlatform.WORKDAY
    assert one.company == "Acme Corp"
    assert one.posted_at == datetime(2026, 11, 11, tzinfo=timezone.utc)   # exact startDate wins
    assert one.url.endswith("/SWE-I_R1")
    assert one.company_slug == "acme.wd1.myworkdayjobs.com/External"

    two = by_title["Software Engineer II"]   # "3 Locations" → detail → New York found
    assert "New York, NY" in two.location
    assert two.url == "https://acme.wd1.myworkdayjobs.com/External/job/x/SWE-II_R2"

    detail_gets = [p for m, p in fake_workday if m == "GET"]
    # Chandler (location fails) and Senior (title fails) never cost a detail call
    assert not any("Chandler" in p or "Sr_R4" in p for p in detail_gets)
    assert len(detail_gets) == 2


def test_malformed_slug_counts_as_failure():
    with pytest.raises(ValueError):
        asyncio.run(WorkdayScraper(CONFIG).scrape_company("evil.com/x"))
