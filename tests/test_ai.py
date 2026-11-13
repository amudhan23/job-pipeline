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

    def fake_writer(label, kind):
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
                        (lambda label, kind: "Draft." if w.is_open_question(label, kind) else None))
    monkeypatch.setattr(ai, "status", lambda *a, **k: (True, ""))

    result = CliRunner().invoke(main, ["apply", "--top", "1", "--submit", "--yes"])
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
