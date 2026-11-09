"""Tests for text utilities."""

from pipeline.utils.text import normalize_company_name, extract_experience_years, matches_keywords


def test_normalize_company_name():
    assert normalize_company_name("Stripe, Inc.") == "stripe"
    assert normalize_company_name("Meta Platforms, Inc") == "meta platforms"
    assert normalize_company_name("Cloudflare Inc") == "cloudflare"
    assert normalize_company_name("HashiCorp") == "hashicorp"
    assert normalize_company_name("  Ramp  LLC  ") == "ramp"


def test_extract_experience_years():
    assert extract_experience_years("3-5 years of experience") == (3, 5)
    assert extract_experience_years("5+ years") == (5, 5)
    assert extract_experience_years("at least 3 years") == (3, 3)
    assert extract_experience_years("minimum of 2 years experience") == (2, 2)
    assert extract_experience_years("no experience required") is None


def test_matches_keywords():
    assert matches_keywords("Backend Software Engineer", ["backend", "software"])
    assert not matches_keywords("Frontend Engineer", ["backend"])
    assert not matches_keywords("Senior Backend Engineer", ["backend"], exclude=["senior"])
    assert matches_keywords("Backend Engineer", ["backend"], exclude=["senior"])
