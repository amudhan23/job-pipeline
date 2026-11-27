"""Interview odds: the posting's age, the company's size, the level asked,
sponsorship and location, each explained."""
from datetime import datetime, timedelta

from pipeline.odds import odds

NOW = datetime(2026, 11, 26, 12, 0)


def _job(**k):
    base = {"title": "Software Engineer", "company": "Acme", "platform": "greenhouse", "location": "New York, NY",
            "first_seen_at": (NOW - timedelta(hours=2)).isoformat(), "posted_at": None, "baseline": 0,
            "description": "We are hiring. 2-4 years of experience with Java.", "h1b_filings_count": 12, "fit_score": 80}
    base.update(k)
    return base


def test_fresh_right_level_sponsoring_posting_is_a_good_bet():
    o = odds(_job(), years=3.5, now=NOW)
    assert o["label"] == "good" and o["score"] >= 80
    assert any("opened 2h ago" in w for w in o["why"]) and any("asks 2-4 years" in w for w in o["why"])
    assert any("sponsors" in w for w in o["why"])


def test_age_and_size_and_level_move_the_odds():
    old = odds(_job(first_seen_at=(NOW - timedelta(days=12)).isoformat()), years=3.5, now=NOW)
    assert old["label"] != "good" and old["score"] < odds(_job(), years=3.5, now=NOW)["score"] - 25
    assert any("12 days old" in w for w in old["why"])
    startup = odds(_job(platform="ashby"), years=3.5, now=NOW)
    enterprise = odds(_job(platform="workday"), years=3.5, now=NOW)
    assert startup["score"] > odds(_job(), years=3.5, now=NOW)["score"] > enterprise["score"]
    senior = odds(_job(description="7+ years of experience required"), years=3.5, now=NOW)
    assert senior["label"] != "good" and any("asks 7+ years" in w for w in senior["why"])
    grad = odds(_job(title="Software Engineer, New Grad", description="Join our 2026 class."), years=3.5, now=NOW)
    assert any("new-grad posting" in w for w in grad["why"]) and grad["score"] < odds(_job(), years=3.5, now=NOW)["score"]
    mid = odds(_job(title="Software Engineer II", description="Build things."), years=3.5, now=NOW)
    assert any("mid-level title" in w for w in mid["why"])


def test_no_sponsorship_is_a_long_shot_and_remote_costs_a_little():
    blocked = odds(_job(jd_blocked=True), years=3.5, now=NOW)
    assert blocked["label"] == "long shot" and "posting says no sponsorship" in blocked["why"]
    remote = odds(_job(location="Remote (US)"), years=3.5, now=NOW)
    assert any("remote" in w for w in remote["why"]) and remote["score"] < odds(_job(), years=3.5, now=NOW)["score"]


def test_baseline_without_a_company_date_has_unknown_age():
    o = odds(_job(baseline=1, first_seen_at=(NOW - timedelta(days=30)).isoformat()), years=3.5, now=NOW)
    assert "age unknown" in o["why"]
