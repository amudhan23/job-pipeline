"""Tests for search-based slug discovery."""

from pipeline.scrapers.search_discover import (
    generate_queries,
    _extract_slug,
    _build_role_clause,
    _build_location_clause,
)


CONFIG = {
    "filters": {
        "role_keywords": ["software engineer", "backend engineer"],
        "locations": ["New York", "San Francisco", "Remote"],
    }
}


class TestQueryGeneration:
    def test_generates_one_query_per_domain(self):
        queries = generate_queries(CONFIG)
        # At least the three scrapable platforms present
        platforms = {p for p, _ in queries}
        assert "greenhouse" in platforms
        assert "ashby" in platforms
        assert "lever" in platforms

    def test_query_has_site_prefix(self):
        queries = generate_queries(CONFIG, platforms=["ashby"])
        assert len(queries) == 1
        _, q = queries[0]
        assert q.startswith("site:jobs.ashbyhq.com")

    def test_query_includes_roles(self):
        _, q = generate_queries(CONFIG, platforms=["lever"])[0]
        assert '"software engineer"' in q
        assert " OR " in q

    def test_no_location_flag(self):
        _, q = generate_queries(CONFIG, platforms=["lever"], include_location=False)[0]
        assert "New York" not in q

    def test_location_drops_remote(self):
        clause = _build_location_clause(["Remote"])
        assert clause == ""  # "remote" filtered out as noise

    def test_platform_filter(self):
        queries = generate_queries(CONFIG, platforms=["greenhouse"])
        assert all(p == "greenhouse" for p, _ in queries)


class TestSlugExtraction:
    def test_greenhouse(self):
        assert _extract_slug("https://boards.greenhouse.io/stripe/jobs/1") == ("greenhouse", "stripe")

    def test_greenhouse_new_domain(self):
        assert _extract_slug("https://job-boards.greenhouse.io/figma/jobs/2") == ("greenhouse", "figma")

    def test_ashby(self):
        assert _extract_slug("https://jobs.ashbyhq.com/openai/uuid") == ("ashby", "openai")

    def test_lever(self):
        assert _extract_slug("https://jobs.lever.co/spotify/uuid") == ("lever", "spotify")

    def test_smartrecruiters_lowercases(self):
        assert _extract_slug("https://jobs.smartrecruiters.com/Square/123") == ("smartrecruiters", "square")

    def test_blocklist_filters_embed(self):
        assert _extract_slug("https://boards.greenhouse.io/embed/job_app") is None

    def test_non_ats_url(self):
        assert _extract_slug("https://linkedin.com/jobs/123") is None

    def test_empty(self):
        assert _extract_slug("") is None


class TestClauses:
    def test_role_clause_empty(self):
        assert _build_role_clause([]) == ""

    def test_role_clause_single(self):
        assert _build_role_clause(["swe"]) == '("swe")'

    def test_role_clause_multiple(self):
        assert _build_role_clause(["a", "b"]) == '("a" OR "b")'
