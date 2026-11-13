"""Claude API integration — all offline: a fake client stands in for the API."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.ai import client as ai
from pipeline.ai import writer

KEY = "sk-ant-api03-" + "x" * 40


class FakeClient:
    """Records requests; answers with fixed text and token counts."""
    def __init__(self, text="I built X at IBM.", inp=1500, out=120):
        self.calls = []
        self.text, self.inp, self.out = text, inp, out
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(text=self.text)],
            usage=SimpleNamespace(input_tokens=self.inp, output_tokens=self.out,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    from pipeline import applicant
    d = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", d)
    return d


def test_key_saved_private_and_masked(pdir):
    with pytest.raises(ValueError):
        ai.save_key("not-a-key", pdir)
    ai.save_key(KEY, pdir)
    assert ai.get_key(pdir) == KEY
    assert oct(os.stat(pdir / ai.KEY_FILE).st_mode)[-3:] == "600"
    assert ai.masked_key(pdir) == "sk-ant-…xxxx" and KEY not in ai.masked_key(pdir)
    ai.forget_key(pdir)
    assert ai.get_key(pdir) is None


def test_env_key_wins(pdir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert ai.get_key(pdir) == KEY


def test_cost_math():
    # Sonnet 5.5: $2 in / $10 out per million
    assert ai.cost_of("claude-sonnet-5-5", {"input_tokens": 1_000_000}) == pytest.approx(2.0)
    assert ai.cost_of("claude-sonnet-5-5", {"output_tokens": 1000}) == pytest.approx(0.01)
    assert ai.cost_of("claude-haiku-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) \
        == pytest.approx(0.60)


def test_call_is_logged_and_budget_enforced(pdir):
    ai.save_key(KEY, pdir)
    ai.save_settings({"monthly_budget": 0.02}, pdir)
    fake = FakeClient(inp=2000, out=200)          # 2000*2 + 200*10 = $0.006
    r = ai.complete("sys", "hello", purpose="t", max_tokens=200, private_dir=pdir, client=fake)
    assert r.text == "I built X at IBM." and r.cost == pytest.approx(0.006)
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    s = ai.spend_summary(pdir)
    assert s["month_calls"] == 1 and s["month_spent"] == pytest.approx(0.006)
    ai.complete("sys", "hello", purpose="t", max_tokens=200, private_dir=pdir, client=fake)
    ai.complete("sys", "hello", purpose="t", max_tokens=200, private_dir=pdir, client=fake)
    # $0.018 spent — the next call's worst case doesn't fit in $0.002
    with pytest.raises(ai.AIUnavailable):
        ai.complete("sys", "hello", purpose="t", max_tokens=200, private_dir=pdir, client=fake)
    assert len(fake.calls) == 3


def test_status_explains_what_is_missing(pdir):
    ready, why = ai.status(pdir)
    assert not ready and "key" in why
    ai.save_key(KEY, pdir)
    ai.save_settings({"enabled": False}, pdir)
    assert "switched off" in ai.status(pdir)[1]


@pytest.mark.parametrize("label,kind,expected", [
    ("Why Ande?", "textarea", True),
    ("Why Ande?", "text", True),
    ("What exceptional work have you done?*", "textarea", True),
    ("Tell us about a project you're proud of", "text", True),
    ("Please indicate your SpaceXSI employment history.*", "textarea", False),
    ("Will you require visa sponsorship?", "textarea", False),
    ("What are your salary expectations?", "text", False),
    ("Current company", "text", False),
    ("How did you hear about us?", "text", False),
    ("Why Ande?", "combobox", False),
    ("Describe your gender identity", "textarea", False),
    ("Please describe how you manage competing priorities", "textarea", True),  # "manage" ≠ age
])
def test_open_question_detection(label, kind, expected):
    assert writer.is_open_question(label, kind) is expected


def test_draft_is_grounded_and_cached(pdir):
    ai.save_key(KEY, pdir)
    fake = FakeClient(text='"At IBM I cut deploy time 40%; Ande\'s AI infra work is the same problem."')
    job = {"company": "Ande", "title": "Software Engineer, AI",
           "description": "<p>Build <b>LLM</b> infrastructure</p>"}
    details = {"first_name": "Amudhan", "school": "New York University"}
    text = writer.draft_answer("Why Ande?", job, details, "IBM — Software Engineer — cut deploy time 40%",
                               pdir, client=fake)
    assert text.startswith("At IBM") and not text.startswith('"')
    call = fake.calls[0]
    blocks = call["system"]
    assert "ONLY facts" in blocks[0]["text"]
    assert "cut deploy time 40%" in blocks[1]["text"] and blocks[1]["cache_control"]["type"] == "ephemeral"
    assert "LLM infrastructure" in call["messages"][0]["content"] and "<p>" not in call["messages"][0]["content"]
    # second time: from the cache, no API call
    again = writer.draft_answer("Why Ande?", job, details, "resume", pdir, client=fake)
    assert again == text and len(fake.calls) == 1


def test_writer_needs_key_and_resume(pdir):
    from pipeline import applicant
    job = {"company": "Ande", "title": "SWE"}
    assert writer.make_writer(job, {}, pdir) is None            # no key
    ai.save_key(KEY, pdir)
    assert writer.make_writer(job, {}, pdir) is None            # no resume to ground it in


# ── In the browser ─────────────────────────────────────────────────────

def _browser_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("google-chrome") or shutil.which("chromium"))


ASHBY_WHY = Path(__file__).parent / "fixtures" / "forms" / "ashby_why.html"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_ai_fills_only_open_required_questions(tmp_path):
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form
    asked = []

    def fake_writer(label, kind, options=None):
        asked.append(label)
        return "Draft answer." if writer.is_open_question(label, kind) else None

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company="Ande", title="SWE", url="", mode="practice")
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.goto(ASHBY_WHY.resolve().as_uri())
        fill_form(page, {"first_name": "A", "last_name": "M", "email": "a@x.com"}, resume, report,
                  writer=fake_writer)
        why = page.eval_on_selector("#why", "e => e.value")
        extra = page.eval_on_selector("#extra", "e => e.value")
        browser.close()
    assert why == "Draft answer." and extra == ""                  # optional box left alone
    assert report.ai_written == ["Why Ande?*"]
    assert "Anything else you'd like to share?" not in asked
    assert report.ready_to_submit


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_yes_does_not_auto_submit_ai_drafts(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline import applicant
    from pipeline.apply import filler
    from pipeline.cli import main
    from pipeline.models import ATSPlatform, Job
    from pipeline.tracker import db as trackerdb
    pdir = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", pdir)
    monkeypatch.setattr(trackerdb, "DEFAULT_DB_PATH", tmp_path / "t.db")
    applicant.save({**applicant.defaults(), "first_name": "A", "last_name": "M", "email": "a@x.com",
                    "phone": "646-555-0123", "city": "New York, NY", "authorized_us": "Yes",
                    "sponsorship_now": "No", "sponsorship_future": "Yes"}, pdir)
    applicant.save_resume(b"%PDF-1.4 test", pdir)
    t = trackerdb.Tracker()
    job = Job(id="ab-ande-1", title="SWE", company="Ande", company_slug="ande", location="NYC",
              url="https://jobs.ashbyhq.com/ande/1", platform=ATSPlatform.ASHBY)
    job.fit_score = 80
    t.upsert_jobs([job], run_id=t.start_run(7))
    monkeypatch.setattr(filler, "application_url", lambda j: ASHBY_WHY.resolve().as_uri())
    import pipeline.ai.writer as w
    monkeypatch.setattr(w, "make_writer", lambda job, details, *a, **k:
                        (lambda label, kind, options=None: "Draft." if w.is_open_question(label, kind) else None))
    monkeypatch.setattr(ai, "status", lambda *a, **k: (True, ""))

    result = CliRunner().invoke(main, ["apply", "--top", "1", "--submit", "--yes", "--no-fit", "--no-tailor"])
    assert result.exit_code == 0, result.output
    assert "AI draft" in result.output and "not auto-submitting" in result.output
    assert trackerdb.Tracker().get_job("ab-ande-1")["status"] != "applied"
    pending = applicant.load_pending(pdir)
    assert pending["Ande"][applicant.normalize_question("Why Ande?*", "Ande")]["draft"] == "Draft."


# ── Dashboard ──────────────────────────────────────────────────────────

def test_dashboard_never_returns_the_key(tmp_path):
    import threading
    import urllib.request
    from http.server import HTTPServer
    from pipeline.dashboard.app import DashboardHandler
    DashboardHandler.private_dir = tmp_path / "private"
    srv = HTTPServer(("127.0.0.1", 0), DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(f"{base}/api/ai", method="POST",
                                     data=json.dumps({"key": KEY, "monthly_budget": 3}).encode(),
                                     headers={"Content-Type": "application/json"})
        body = urllib.request.urlopen(req).read().decode()
        assert KEY not in body and json.loads(body)["key"] == "sk-ant-…xxxx"
        got = json.loads(urllib.request.urlopen(f"{base}/api/ai").read())
        assert got["settings"]["monthly_budget"] == 3 and KEY not in json.dumps(got)
        bad = urllib.request.Request(f"{base}/api/ai", method="POST", data=b'{"key":"nope"}',
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        srv.shutdown()
        DashboardHandler.private_dir = None


# ── Fit check ──────────────────────────────────────────────────────────

XAI_JD = ("Government Cloud Security Engineer. Must be a U.S. person as defined by ITAR. "
          "Active Secret clearance required. 3+ years experience.")


def test_fit_check_skip_needs_quoted_evidence(pdir):
    from pipeline.ai import fitcheck
    ai.save_key(KEY, pdir)
    job = {"id": "gh-xai-1", "company": "xAI", "title": "Gov Cloud Security", "description": XAI_JD}
    fake = FakeClient(text=json.dumps({"verdict": "skip", "dealbreakers": [
        {"rule": "clearance", "quote": "Active Secret clearance required"}],
        "concerns": [], "summary": "Needs an active clearance."}))
    r = fitcheck.check_job(job, {"citizenship": "India"}, "resume", pdir, client=fake)
    assert r["verdict"] == "skip" and r["dealbreakers"][0]["quote"].startswith("Active Secret")
    assert fake.calls[0]["model"] == "claude-haiku-5-5"            # always the cheap model
    assert "citizenship: India" in fake.calls[0]["system"][1]["text"]
    # "skip" without a quote is downgraded — never throw a job away on a hunch
    vague = FakeClient(text='{"verdict": "skip", "dealbreakers": [], "summary": "feels senior"}')
    assert fitcheck.check_job(job, {}, "resume", pdir, client=vague)["verdict"] == "maybe"
    garbage = FakeClient(text="I think you should apply!")
    assert fitcheck.check_job(job, {}, "resume", pdir, client=garbage)["verdict"] == "maybe"


def test_fit_check_stored_once(pdir, tmp_path):
    from pipeline.ai import fitcheck
    from pipeline.models import ATSPlatform, Job
    from pipeline.tracker.db import Tracker
    ai.save_key(KEY, pdir)
    t = Tracker(tmp_path / "t.db")
    t.upsert_jobs([Job(id="j1", title="SWE", company="Acme", company_slug="acme", location="NYC",
                       url="u", platform=ATSPlatform.ASHBY, description="Build APIs in Java.")],
                  run_id=t.start_run(7))
    fake = FakeClient(text='{"verdict": "apply", "dealbreakers": [], "summary": "Java backend, fits."}')
    job = t.get_job("j1")
    assert fitcheck.ensure_checked(job, t, {}, "resume", pdir, client=fake)["verdict"] == "apply"
    again = t.get_job("j1")
    assert fitcheck.stored(again)["verdict"] == "apply"
    fitcheck.ensure_checked(again, t, {}, "resume", pdir, client=fake)
    assert len(fake.calls) == 1
    # a re-scrape keeps the result (like status)
    t.upsert_jobs([Job(id="j1", title="SWE", company="Acme", company_slug="acme", location="NYC",
                       url="u", platform=ATSPlatform.ASHBY, description="Build APIs in Java.")],
                  run_id=t.start_run(7))
    assert fitcheck.stored(t.get_job("j1"))["verdict"] == "apply"


def test_no_description_is_not_sent(pdir):
    from pipeline.ai import fitcheck
    ai.save_key(KEY, pdir)
    fake = FakeClient()
    r = fitcheck.check_job({"id": "x", "company": "A", "description": ""}, {}, "resume", pdir, client=fake)
    assert r["verdict"] == "maybe" and not fake.calls


# ── AI picks for closed questions ──────────────────────────────────────

def test_pick_from_resume(pdir):
    ai.save_key(KEY, pdir)
    job = {"company": "Affirm", "title": "SWE", "description": "Java services"}
    fake = FakeClient(text='{"answer": "3-5 years", "basis": "IBM Application Developer Jul 2021 - Jul 2024"}')
    a = writer.pick_answer("Years of professional Java experience?*", "select",
                           ["0-2 years", "3-5 years", "5+ years"], job, {}, "IBM ... Java", pdir, client=fake)
    assert a.value == "3-5 years" and a.how == "pick" and "IBM" in a.source


def test_pick_unknown_or_invalid_is_left_for_you(pdir):
    ai.save_key(KEY, pdir)
    job = {"company": "Twitch", "title": "SWE", "description": ""}
    unk = FakeClient(text='{"answer": "UNKNOWN", "basis": ""}')
    assert writer.pick_answer("How would you describe your experience with Twitch?", "combobox",
                              ["I watch streams", "I stream"], job, {}, "r", pdir, client=unk) is None
    # cached: asking again costs nothing
    writer.pick_answer("How would you describe your experience with Twitch?", "combobox",
                       ["I watch streams", "I stream"], job, {}, "r", pdir, client=unk)
    assert len(unk.calls) == 1
    made_up = FakeClient(text='{"answer": "Daily viewer", "basis": "resume"}')       # not an option
    assert writer.pick_answer("Platform usage?", "select", ["Yes", "No"], job, {}, "r", pdir,
                              client=made_up) is None


def test_pick_not_in_resume_says_so(pdir):
    ai.save_key(KEY, pdir)
    job = {"company": "Twitch", "title": "SWE", "description": ""}
    fake = FakeClient(text='{"answer": "No", "basis": "not in resume"}')
    a = writer.pick_answer("Do you have hands-on experience in SAP?*", "combobox", ["Yes", "No"],
                           job, {}, "r", pdir, client=fake)
    assert a.value == "No" and "not in your resume" in a.source


@pytest.mark.parametrize("label", [
    "Have you previously applied to Amazon or any Amazon subsidiary?*",
    "Who referred you?", "Do you know anyone who works at Twitch?",
    "Will you require sponsorship?", "Gender", "I certify the above is true",
])
def test_never_picked(label):
    assert not writer.can_pick(label, "combobox")


# ── Tailored resume ────────────────────────────────────────────────────

def _config():
    import yaml
    return yaml.safe_load((Path(__file__).parents[1] / "config" / "profile.yaml").read_text())


def test_tailor_only_reorders(pdir):
    from pipeline.resume import tailor
    ai.save_key(KEY, pdir)
    reply = {"ibm": ["db", "own", "bogus"], "sahara": ["int"], "projects": ["Commentator", "req-guard", "Made Up"],
             "languages": ["Python", "Cobol"], "note": "Data-heavy role: led with databases."}
    fake = FakeClient(text=json.dumps(reply))
    rc, note = tailor.ai_config({"id": "j", "company": "A", "title": "SWE", "description": "Postgres"},
                                _config(), pdir, client=fake)
    assert rc.ibm_bullet_order == ["db", "own", "mig", "dbg", "ci"]      # bogus dropped, none lost
    assert rc.sahara_bullet_order[0] == "int" and len(rc.sahara_bullet_order) == 3
    assert rc.projects == ["Commentator", "req-guard", "AccessMint"]     # invented project ignored
    assert rc.language_order[0] == "Python" and "Cobol" not in rc.language_order
    assert "databases" in note


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex needed")
def test_tailored_resume_builds_and_is_reused(pdir, tmp_path):
    from pipeline.resume import tailor
    ai.save_key(KEY, pdir)
    fake = FakeClient(text=json.dumps({"ibm": ["db"], "sahara": [], "projects": ["Commentator"],
                                       "languages": ["Python"], "note": "AI role."}))
    job = {"id": "ab-ande-1", "company": "Ande", "title": "SWE, AI", "description": "LLM agents",
           "platform": "ashby", "url": "u"}
    folder = tmp_path / "app"
    pdf, note = tailor.tailored_resume(job, _config(), {"first_name": "Amudhan", "last_name": "Muthaiah"},
                                       folder, private_dir=pdir, client=fake)
    assert pdf.name == "Amudhan_Muthaiah_Resume.pdf" and pdf.read_bytes()[:4] == b"%PDF"
    from pipeline.applicant import resume_text
    text = resume_text(pdf.read_bytes())
    assert text.index("Commentator") < text.index("req-guard")           # AI's lead project first
    assert "Python" in text and note == "AI role."
    again, _ = tailor.tailored_resume(job, _config(), {"first_name": "Amudhan", "last_name": "Muthaiah"},
                                      folder, private_dir=pdir, client=fake)
    assert again == pdf and len(fake.calls) == 1                          # reused, no new call


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_apply_skips_dealbreaker_and_moves_on(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline import applicant
    from pipeline.ai import fitcheck
    from pipeline.apply import filler
    from pipeline.cli import main
    from pipeline.models import ATSPlatform, Job
    from pipeline.tracker import db as trackerdb
    pdir = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", pdir)
    monkeypatch.setattr(trackerdb, "DEFAULT_DB_PATH", tmp_path / "t.db")
    applicant.save({**applicant.defaults(), "first_name": "A", "last_name": "M", "email": "a@x.com",
                    "phone": "646-555-0123", "city": "New York, NY", "authorized_us": "Yes",
                    "sponsorship_now": "No", "sponsorship_future": "Yes"}, pdir)
    applicant.save_resume(b"%PDF-1.4 test", pdir)
    t = trackerdb.Tracker()
    run = t.start_run(7)
    for jid, company, score in (("gh-xai-1", "xAI", 90), ("ab-ande-1", "Ande", 80)):
        j = Job(id=jid, title="SWE", company=company, company_slug=company.lower(), location="NYC",
                url=f"https://jobs.ashbyhq.com/{company.lower()}/1", platform=ATSPlatform.ASHBY)
        j.fit_score = score
        t.upsert_jobs([j], run_id=run)
    monkeypatch.setattr(filler, "application_url", lambda j: ASHBY_WHY.resolve().as_uri() + "#" + j["id"])
    monkeypatch.setattr(ai, "status", lambda *a, **k: (True, ""))
    import pipeline.ai.writer as w
    monkeypatch.setattr(w, "make_writer", lambda *a, **k: None)
    verdicts = {"gh-xai-1": {"verdict": "skip", "summary": "clearance",
                             "dealbreakers": [{"rule": "clearance", "quote": "Active Secret clearance required"}]},
                "ab-ande-1": {"verdict": "apply", "summary": "fits", "dealbreakers": []}}
    monkeypatch.setattr(fitcheck, "ensure_checked", lambda job, *a, **k: verdicts[job["id"]])

    result = CliRunner().invoke(main, ["apply", "--top", "1", "--no-tailor"])
    assert result.exit_code == 0, result.output
    assert "skipped by fit check" in result.output and "Active Secret clearance" in result.output
    assert "Ande" in result.output and "filled" in result.output        # moved on to fill 1 job
    assert trackerdb.Tracker().get_job("gh-xai-1")["status"] == "skipped"
