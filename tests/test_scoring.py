"""Tests for the fit scorer."""

import pytest
from pipeline.models import Job, ATSPlatform, SponsorshipLikelihood
from pipeline.scoring.scorer import FitScorer


@pytest.fixture
def config():
    return {
        "stack": {
            "languages": {
                "primary": ["Java", "Rust", "TypeScript", "Python"],
                "secondary": ["C++", "JavaScript", "C", "SQL", "Bash"],
            },
            "frameworks": ["Spring Boot", "React", "Node.js", "Express"],
            "databases": ["PostgreSQL", "MySQL", "Redis", "SQLite"],
            "cloud": ["Azure", "AWS", "GCP", "Docker", "Kubernetes"],
            "tools": ["Jenkins", "Git", "Linux", "Terraform"],
        },
        "scoring": {
            "stack_match": 0.30,
            "experience_alignment": 0.15,
            "sponsorship_likelihood": 0.25,
            "location_match": 0.15,
            "role_type_match": 0.15,
        },
        "filters": {"max_experience": 4},
    }


@pytest.fixture
def scorer(config):
    return FitScorer(config)


def _make_job(**kwargs) -> Job:
    defaults = {
        "id": "test-1",
        "title": "Software Engineer",
        "company": "TestCo",
        "company_slug": "testco",
        "location": "New York, NY",
        "url": "https://example.com",
        "platform": ATSPlatform.GREENHOUSE,
    }
    defaults.update(kwargs)
    return Job(**defaults)


def test_high_fit_job(scorer):
    from datetime import datetime, timezone
    job = _make_job(
        title="Backend Software Engineer",
        location="New York, NY",
        description="We use Java, Spring Boot, PostgreSQL, Redis, Docker, Kubernetes, and AWS.",
        sponsorship=SponsorshipLikelihood.HIGH,
        posted_at=datetime.now(timezone.utc),  # great on every dimension, incl. freshness
    )
    score = scorer.score(job)
    assert score > 70, f"Expected > 70 for a great fit, got {score}"


def test_low_fit_job(scorer):
    job = _make_job(
        title="Senior iOS Engineer",
        location="Austin, TX",
        description="Looking for 8+ years experience with Swift, UIKit, and Core Data.",
        sponsorship=SponsorshipLikelihood.LOW,
    )
    score = scorer.score(job)
    assert score < 40, f"Expected < 40 for a poor fit, got {score}"


def test_fresher_job_outranks_identical_older_one(scorer):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    base = dict(title="Backend Engineer", description="Java Spring Boot PostgreSQL",
                sponsorship=SponsorshipLikelihood.HIGH)
    today = _make_job(id="today", posted_at=now - timedelta(hours=4), **base)
    stale = _make_job(id="stale", posted_at=now - timedelta(days=25), **base)
    ranked = scorer.score_jobs([stale, today])
    assert [j.id for j in ranked] == ["today", "stale"]
    assert today.fit_breakdown["recency"] == 100
    assert stale.fit_breakdown["recency"] == 35


def test_undated_job_gets_neutral_recency(scorer):
    job = _make_job()
    scorer.score(job)
    assert job.fit_breakdown["recency"] == 50


def test_score_stays_within_0_100_with_any_weights():
    s = FitScorer({"scoring": {"stack_match": 5, "recency": 5}})  # odd, unnormalized weights
    job = _make_job(description="Java Rust TypeScript Python")
    assert 0 <= s.score(job) <= 100


def test_scoring_sorts_correctly(scorer):
    good = _make_job(
        id="good",
        title="Backend Engineer",
        location="NYC",
        description="Java Spring Boot PostgreSQL Redis",
        sponsorship=SponsorshipLikelihood.HIGH,
    )
    bad = _make_job(
        id="bad",
        title="Senior ML Engineer",
        location="Seattle",
        description="PyTorch TensorFlow 7+ years",
        sponsorship=SponsorshipLikelihood.LOW,
    )
    ranked = scorer.score_jobs([bad, good])
    assert ranked[0].id == "good"
    assert ranked[1].id == "bad"
