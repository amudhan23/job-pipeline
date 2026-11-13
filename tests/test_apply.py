"""Tests for auto-apply: answer rules and the practice-mode form filler."""

import shutil
from pathlib import Path

import pytest

from pipeline.apply.answers import answer_for, is_consent, pick_option
from pipeline.apply.filler import application_url

FORM = Path(__file__).parent / "fixtures" / "forms" / "application.html"

DETAILS = {
    "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com", "phone": "646-555-0123",
    "linkedin": "https://linkedin.com/in/jane", "city": "New York, NY", "authorized_us": "Yes",
    "sponsorship_now": "No", "sponsorship_future": "Yes", "school": "New York University",
    "relocate": "Yes", "gender": "Decline to self-identify", "veteran": "Decline to self-identify",
}


class TestAnswers:
    @pytest.mark.parametrize("label,expected", [
        ("First Name *", "Jane"),
        ("Full name", "Jane Doe"),
        ("LinkedIn Profile URL", "https://linkedin.com/in/jane"),
        ("Will you now or in the future require sponsorship?", "Yes"),
        ("Do you currently require visa sponsorship?", "No"),
        ("Are you legally authorized to work in the US?", "Yes"),
        ("Are you authorized to work in the US without sponsorship?", "Yes"),  # a sponsorship question
    ])
    def test_rules(self, label, expected):
        assert answer_for(label, DETAILS).value == expected

    def test_unknowns_are_not_guessed(self):
        assert answer_for("Why do you want to work here?", DETAILS) is None
        assert answer_for("Desired salary", DETAILS) is None             # blank in details
        assert answer_for("Tell us about your open-source work", DETAILS) is None
        assert answer_for("Describe a time you showed grace under pressure", DETAILS) is None

    def test_consent_never_answered(self):
        assert is_consent("I certify that the information provided is accurate and complete")
        assert answer_for("I agree to the privacy policy", DETAILS) is None

    def test_pick_option(self):
        assert pick_option("Yes", ["Select...", "Yes, I am authorized", "No"]) == "Yes, I am authorized"
        assert pick_option("Decline to self-identify",
                           ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"
        assert pick_option("Decline to self-identify", ["Male", "Female"]) is None
        assert pick_option("Maybe", ["Yes", "No"]) is None


def test_application_urls():
    assert application_url({"platform": "greenhouse", "id": "gh-stripe-123",
                            "url": "https://stripe.com/jobs/listing/x?gh_jid=123"}) \
        == "https://job-boards.greenhouse.io/stripe/jobs/123"
    assert application_url({"platform": "lever", "url": "https://jobs.lever.co/spotify/abc"}) \
        == "https://jobs.lever.co/spotify/abc/apply"
    assert application_url({"platform": "ashby", "url": "https://jobs.ashbyhq.com/kalshi/u-1/"}) \
        == "https://jobs.ashbyhq.com/kalshi/u-1/application"
    assert application_url({"platform": "workday", "url": "https://x"}) is None


# ── Real browser against the practice form ─────────────────────────────

def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    return bool(shutil.which("google-chrome") or shutil.which("chromium"))


def _cli_env(tmp_path, monkeypatch, details):
    from pipeline import applicant
    from pipeline.tracker import db as trackerdb
    from pipeline.models import ATSPlatform, Job
    pdir = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", pdir)
    monkeypatch.setattr(trackerdb, "DEFAULT_DB_PATH", tmp_path / "t.db")
    if details:
        applicant.save({**applicant.defaults(), **details}, pdir)
        applicant.save_resume(b"%PDF-1.4 test", pdir)
    t = trackerdb.Tracker()
    job = Job(id="ab-acme-1", title="Software Engineer", company="Acme", company_slug="acme",
              location="NYC", url="https://jobs.ashbyhq.com/acme/1", platform=ATSPlatform.ASHBY)
    job.fit_score = 80
    t.upsert_jobs([job], run_id=t.start_run(7))
    return pdir


def test_apply_command_requires_details(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline.cli import main
    _cli_env(tmp_path, monkeypatch, details=None)
    result = CliRunner().invoke(main, ["apply"])
    assert result.exit_code != 0 and "Phone" in result.output


def test_apply_command_refuses_submit(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline.cli import main
    _cli_env(tmp_path, monkeypatch, details=DETAILS)
    result = CliRunner().invoke(main, ["apply", "--submit"])
    assert result.exit_code != 0 and "isn't switched on" in result.output


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_apply_command_practice_run(tmp_path, monkeypatch):
    import json
    from click.testing import CliRunner
    from pipeline.apply import filler
    from pipeline.cli import main
    pdir = _cli_env(tmp_path, monkeypatch, details=DETAILS)
    monkeypatch.setattr(filler, "application_url", lambda job: FORM.resolve().as_uri())

    result = CliRunner().invoke(main, ["apply", "--top", "1"])
    assert result.exit_code == 0, result.output
    assert "filled 13 field(s)" in result.output and "needs you" in result.output
    report = json.loads((pdir / "applications" / "ab-acme-1" / "report.json").read_text())
    assert report["mode"] == "practice" and report["ready_to_submit"] is False
    assert (pdir / "applications" / "ab-acme-1" / "filled.png").exists()


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_fill_practice_form(tmp_path):
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company="Acme", title="SWE", url="", mode="practice")

    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.goto(FORM.resolve().as_uri())
        fill_form(page, DETAILS, resume, report)

        v = lambda sel: page.eval_on_selector(sel, "e => e.value")  # noqa: E731
        assert v("#first_name") == "Jane" and v("#phone") == "646-555-0123"
        assert v("#auth") == "Yes"
        assert page.is_checked("input[name=spons][value=y]")              # radio
        assert v("#school") == "New York University"                       # searchable dropdown
        assert page.evaluate("window.relocate") == "Yes"                   # Yes/No buttons
        assert v("#gender") == "Decline To Self Identify"
        assert v("#vet") == "I don't wish to answer"
        assert page.eval_on_selector("#resume", "e => e.files.length") == 1
        assert v("#why") == "" and v("#salary") == ""                      # not guessed
        assert not page.is_checked("#consent")                             # never auto-consent
        assert page.evaluate("window.submitted") is None                   # never submitted
        browser.close()

    attention = {f.label.split("?")[0] for f in report.needs_attention}
    assert attention == {"Why do you want to work at Acme",
                         "I certify that the information provided is accurate and complete *"}
    assert [f.label for f in report.skipped_optional] == ["Desired salary"]
    assert not report.ready_to_submit
