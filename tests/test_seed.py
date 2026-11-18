"""Board seeding from the full SimplifyJobs history, and fair scheduling."""
from datetime import datetime, timedelta

from pipeline.discovery import BoardRegistry
from pipeline.discovery import seed


LISTINGS = [
    {"url": "https://boards.greenhouse.io/stripe/jobs/1", "company_name": "Stripe", "active": False},
    {"url": "https://jobs.lever.co/plaid/abc", "company_name": "Plaid", "active": True},
    {"url": "https://jobs.ashbyhq.com/ramp/xyz", "company_name": "Ramp"},
    {"url": "https://example.com/careers/1", "company_name": "Nope"},
    {"url": "", "company_name": "Empty"},
]


def test_harvest_keeps_closed_and_old_listings():
    boards, names = seed.harvest(LISTINGS)
    assert ("greenhouse", "stripe") in boards        # closed listing still names a board
    assert ("lever", "plaid") in boards and ("ashby", "ramp") in boards
    assert len(boards) == 3
    assert names[("greenhouse", "stripe")] == "Stripe"


def test_due_weekly(tmp_path):
    path = tmp_path / "seed.json"
    assert seed.due(path)
    seed.save({"boards": 3, "new": 3}, path)
    assert not seed.due(path)
    assert seed.due(path, now=datetime.now() + timedelta(days=8))


def test_add_many_counts_only_new(tmp_path):
    reg = BoardRegistry(tmp_path / "r.db")
    assert reg.add_many({("greenhouse", "a"), ("lever", "b")}, "seed") == 2
    assert reg.add_many({("greenhouse", "a"), ("ashby", "c")}, "seed") == 1
    assert reg.add_many(set(), "seed") == 0


def test_slow_backlog_cannot_starve_fast_boards(tmp_path):
    reg = BoardRegistry(tmp_path / "r.db")
    reg.add_many({("workday", f"co{i}") for i in range(100)}, "seed")
    reg.add_many({("greenhouse", "fast1"), ("lever", "fast2")}, "seed")
    now = datetime.now().isoformat(timespec="seconds")
    fast = reg.due(now, limit=40, platforms=("greenhouse", "lever", "ashby"))
    slow = reg.due(now, limit=4, exclude=("greenhouse", "lever", "ashby"))
    assert {b["slug"] for b in fast} == {"fast1", "fast2"}
    assert len(slow) == 4 and all(b["platform"] == "workday" for b in slow)


def test_title_variants_and_mts():
    from pipeline.scrapers.base import norm_title
    from pipeline.orchestrator import Pipeline
    cfg = {"filters": {"role_keywords": ["software engineer", "full stack engineer", "backend developer",
                                         "member of technical staff"],
                       "exclude_keywords": ["senior", "sr", "staff", "intern"], "locations": []}}
    s = next(iter(Pipeline.make_scrapers(cfg).values()))
    assert norm_title("Full-Stack Engineer") == "full stack engineer"
    assert s.passes_title("Fullstack Engineer") and s.passes_title("Back-end Developer")
    assert s.passes_title("SWE II") and s.passes_title("Engineer, Software")
    assert s.passes_title("Member of Technical Staff")
    assert not s.passes_title("Staff Software Engineer") and not s.passes_title("Sr. SWE")
    assert not s.passes_title("Software Engineering Intern")


def _gh_job(i, posted):
    from pipeline.models import ATSPlatform, Job
    return Job(id=f"gh-{i}", title="Software Engineer", company="Acme", company_slug="acme", location="Remote",
               url=f"https://x/{i}", platform=ATSPlatform.GREENHOUSE, description="", posted_at=posted)


def test_first_read_keeps_fresh_roles_new_and_old_ones_quiet(tmp_path):
    from pipeline.tracker.db import Tracker
    t = Tracker(tmp_path / "t.db")
    now = datetime.now()
    jobs = [_gh_job(1, now - timedelta(hours=5)), _gh_job(2, now - timedelta(days=20)), _gh_job(3, None)]
    new, _, _ = t.observe_board("greenhouse", "acme", jobs, baseline=True)
    assert new == ["gh-1"]                                   # posted 5h ago: genuinely new
    j1 = t.get_job("gh-1")
    assert j1["baseline"] == 0 and j1["first_seen_at"].startswith((now - timedelta(hours=5)).isoformat()[:13])
    assert t.get_job("gh-2")["baseline"] == 1 and t.get_job("gh-3")["baseline"] == 1


def test_old_role_that_starts_matching_is_not_new(tmp_path):
    """Your filters widen: a role posted weeks ago on a watched board isn't 'just opened'."""
    from pipeline.tracker.db import Tracker
    t = Tracker(tmp_path / "t.db")
    now = datetime.now()
    t.observe_board("greenhouse", "acme", [_gh_job(1, now - timedelta(days=30))], baseline=True)
    jobs = [_gh_job(1, now - timedelta(days=30)), _gh_job(2, now - timedelta(days=25)), _gh_job(3, now)]
    new, _, _ = t.observe_board("greenhouse", "acme", jobs)
    assert new == ["gh-3"]
    assert t.get_job("gh-2")["baseline"] == 1


def test_harvest_links_from_markdown():
    text = "| Acme | [Apply](https://jobs.ashbyhq.com/acme/123) | <a href=\"https://boards.greenhouse.io/foo/jobs/9\">x</a>"
    assert seed.harvest_links(text) == {("ashby", "acme"), ("greenhouse", "foo")}
