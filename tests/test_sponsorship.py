"""Tests for JD-first sponsorship filter logic."""

import pytest

from pipeline.filters.sponsorship import (
    SponsorshipFilter,
    check_jd_blocks_sponsorship,
)
from pipeline.models import Job, ATSPlatform, SponsorshipLikelihood


# ── JD text scanning tests ──


class TestCheckJDBlocksSponsorship:
    """Test the JD text scanning for no-sponsorship language."""

    def test_no_description(self):
        assert check_jd_blocks_sponsorship("") is False

    def test_clean_jd_passes(self):
        jd = """
        We are looking for a backend software engineer.
        Requirements: Python, PostgreSQL, Kubernetes.
        We offer competitive salary and benefits.
        """
        assert check_jd_blocks_sponsorship(jd) is False

    def test_explicit_no_sponsorship(self):
        jd = "We do not sponsor visas for this position."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_cannot_sponsor(self):
        jd = "Unfortunately, we cannot sponsor work visas at this time."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_will_not_sponsor(self):
        jd = "The company will not sponsor applicants for work visas."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_no_visa_sponsorship(self):
        jd = "No visa sponsorship is available for this role."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_must_be_authorized_without_sponsorship(self):
        jd = """
        Candidates must be legally authorized to work in the
        United States without need for current or future visa sponsorship.
        """
        assert check_jd_blocks_sponsorship(jd) is True

    def test_authorized_without_sponsorship(self):
        jd = "Must be authorized to work in the US without sponsorship."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_sponsorship_not_available(self):
        jd = "Sponsorship is not available for this position."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_h1b_not_available(self):
        jd = "H-1B visa sponsorship is not available."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_positive_sponsorship_does_not_block(self):
        """JD that mentions sponsorship positively should NOT block."""
        jd = "We sponsor H-1B visas for qualified candidates."
        assert check_jd_blocks_sponsorship(jd) is False

    def test_mentions_visa_without_blocking(self):
        """Mentioning visa without blocking language → passes."""
        jd = "Visa sponsorship may be available for exceptional candidates."
        assert check_jd_blocks_sponsorship(jd) is False

    def test_wont_sponsor(self):
        jd = "We won't sponsor work visas for this role."
        assert check_jd_blocks_sponsorship(jd) is True

    def test_not_offer_h1b(self):
        jd = "We do not offer H1B visa for this position."
        assert check_jd_blocks_sponsorship(jd) is True


# ── SponsorshipFilter integration tests ──


def _make_job(
    description: str = "",
    company: str = "TestCo",
    **kwargs,
) -> Job:
    return Job(
        id="test-1",
        title="Software Engineer",
        company=company,
        company_slug="testco",
        location="New York, NY",
        url="https://example.com/job/1",
        platform=ATSPlatform.GREENHOUSE,
        description=description,
        **kwargs,
    )


class TestSponsorshipFilterToggle:
    """Test the enabled/disabled toggle."""

    def test_disabled_skips_everything(self):
        sf = SponsorshipFilter(enabled=False)
        job = _make_job("We cannot sponsor visas.")
        jobs = sf.enrich_jobs([job])
        # When disabled, jd_blocks_sponsorship should stay False
        assert jobs[0].jd_blocks_sponsorship is False

    def test_enabled_checks_jd(self):
        sf = SponsorshipFilter(enabled=True)
        job = _make_job("We cannot sponsor visas.")
        jobs = sf.enrich_jobs([job])
        assert jobs[0].jd_blocks_sponsorship is True

    def test_filter_blocked_when_disabled(self):
        sf = SponsorshipFilter(enabled=False)
        job = _make_job("We cannot sponsor visas.")
        sf.enrich_jobs([job])
        # Even if we manually set jd_blocks_sponsorship, filter_blocked
        # should pass everything when disabled
        job.jd_blocks_sponsorship = True
        result = sf.filter_blocked([job])
        assert len(result) == 1


class TestSponsorshipFilterEnrich:
    """Test job enrichment with JD-first logic."""

    def test_clean_jd_passes(self):
        sf = SponsorshipFilter()
        job = _make_job("Looking for a Python backend engineer.")
        sf.enrich_jobs([job])
        assert job.jd_blocks_sponsorship is False

    def test_blocked_jd_marked(self):
        sf = SponsorshipFilter()
        job = _make_job("Must be authorized to work without sponsorship.")
        sf.enrich_jobs([job])
        assert job.jd_blocks_sponsorship is True

    def test_filter_blocked_removes_jd_blocked(self):
        sf = SponsorshipFilter()
        jobs = [
            _make_job("Great opportunity for backend engineers!", company="GoodCo"),
            _make_job("We do not sponsor visas.", company="NoCo"),
            _make_job("Join our team!", company="OkCo"),
        ]
        sf.enrich_jobs(jobs)
        eligible = sf.filter_blocked(jobs)
        assert len(eligible) == 2
        assert all(not j.jd_blocks_sponsorship for j in eligible)

    def test_h1b_data_is_supplementary_only(self):
        """H-1B LOW likelihood should NOT block a job if JD is clean."""
        sf = SponsorshipFilter()
        job = _make_job("Great backend role, Python and PostgreSQL.")
        sf.enrich_jobs([job])
        # Even though H-1B data is UNKNOWN (no records), job passes
        assert job.jd_blocks_sponsorship is False
        eligible = sf.filter_blocked([job])
        assert len(eligible) == 1
