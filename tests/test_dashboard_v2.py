"""New dashboard API, remote-mode auth, auto-apply picking, background runs, handoff."""

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.models import ATSPlatform, Job
from pipeline.tracker.db import Tracker


def _job(i, company="Acme", score=80, platform=ATSPlatform.GREENHOUSE):
    j = Job(id=f"gh-{company.lower()}-{i}", title="Software Engineer", company=company,
            company_slug=company.lower(), location="New York, NY", url=f"https://x/{i}",
            platform=platform, description="Java")
    j.fit_score = score
    return j


@pytest.fixture
def server(tmp_path, monkeypatch):
    from pipeline import applicant
    from pipeline.dashboard.app import DashboardHandler
    from pipeline.discovery import registry
    from pipeline.crawler import worker
    pdir = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", pdir)
    monkeypatch.setattr(registry, "DEFAULT_DB_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(worker, "STATUS_FILE", tmp_path / "status.json")
    t = Tracker(tmp_path / "t.db")
    t.observe_board("greenhouse", "acme", [_job(1)], baseline=True)
    t.observe_board("greenhouse", "acme", [_job(1), _job(2, score=90)])        # job 2 is new
    DashboardHandler.db_path = tmp_path / "t.db"
    DashboardHandler.private_dir = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, t, pdir
    srv.shutdown()


def get(url, headers=None):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})))


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    return json.load(urllib.request.urlopen(req))


def test_feed_has_freshness_and_new_jobs_first(server):
    base, t, _ = server
    feed = get(f"{base}/api/feed?hours=24")
    ids = [j["id"] for j in feed["jobs"]]
    assert ids == ["gh-acme-2"]                         # the baseline job isn't "opened today"
    j = feed["jobs"][0]
    assert j["age_basis"] == "seen" and j["age_min"] < 2 and j["apply_supported"]
    assert {x["id"] for x in get(f"{base}/api/feed?hours=all")["jobs"]} == {"gh-acme-1", "gh-acme-2"}
    s = get(f"{base}/api/summary")
    assert s["new_today"] == 1 and s["applied"] == 0


def test_status_update_and_applications_list(server):
    base, t, pdir = server
    post(f"{base}/api/jobs/gh-acme-2/status", {"status": "applied"})
    apps = get(f"{base}/api/applications")["items"]
    assert [a["id"] for a in apps] == ["gh-acme-2"] and apps[0]["status"] == "applied"
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/jobs/gh-acme-2/status", {"status": "hired!"})
    assert e.value.code == 400


def test_attention_lists_prepared_forms(server):
    base, t, pdir = server
    from pipeline.apply.filler import FieldResult, FillReport
    r = FillReport(job_id="gh-acme-2", company="Acme", title="SWE", url="", mode="practice")
    r.filled = [FieldResult("Name*", "text", True, "A M", "names")]
    r.needs_attention = [FieldResult("Why Acme?", "textarea", True, "", "no saved answer for this question")]
    r.save(pdir / "applications" / "gh-acme-2")
    att = get(f"{base}/api/attention")
    assert len(att["items"]) == 1 and att["items"][0]["needs"][0]["label"] == "Why Acme?"
    assert att["ai_ready"] is False                         # no key in tests
    post(f"{base}/api/jobs/gh-acme-2/status", {"status": "applied"})
    assert get(f"{base}/api/attention")["items"] == []     # applied → off the list


def test_autoapply_settings_validated(server):
    base, _, _ = server
    got = post(f"{base}/api/autoapply", {"enabled": True, "mode": "submit", "min_score": "70", "max_per_day": 5000})
    assert got["settings"]["enabled"] and got["settings"]["min_score"] == 70 and got["settings"]["max_per_day"] == 1000
    with pytest.raises(urllib.error.HTTPError):
        post(f"{base}/api/autoapply", {"mode": "spray-and-pray"})


def test_run_rejects_bad_ids_and_headed_without_screen(server, monkeypatch):
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/apply/run", {"job_ids": ["gh-acme-2; rm -rf /"]})
    assert e.value.code == 400
    monkeypatch.setenv("JOBPIPE_HEADLESS_ONLY", "1")
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/apply/run", {"job_ids": ["gh-acme-2"], "mode": "handoff"})
    assert e.value.code == 400 and "screen" in json.loads(e.value.read())["error"]


def test_files_cannot_escape_the_applications_folder(server):
    base, _, pdir = server
    (pdir / "applications" / "gh-acme-2").mkdir(parents=True)
    (pdir / "applications" / "gh-acme-2" / "filled.png").write_bytes(b"\x89PNG")
    (pdir / "details.json").write_text("{}")
    assert urllib.request.urlopen(f"{base}/files/gh-acme-2/filled.png").read() == b"\x89PNG"
    for bad in ("/files/../details.json", "/files/gh-acme-2/..%2F..%2Fdetails.json", "/files/x/secret.json"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + bad)
        assert e.value.code == 404


def test_static_pages_served(server):
    base, _, _ = server
    html = urllib.request.urlopen(base + "/").read().decode()
    assert "/static/app.js" in html
    assert b"pageJobs" in urllib.request.urlopen(base + "/static/app.js").read()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/static/../app.py")


# ── Remote mode ────────────────────────────────────────────────────────

def test_token_mode_requires_sign_in(server, monkeypatch):
    base, _, _ = server
    monkeypatch.setenv("JOBPIPE_DASHBOARD_TOKEN", "s3cret-token")
    with pytest.raises(urllib.error.HTTPError) as e:
        get(f"{base}/api/summary")
    assert e.value.code == 401
    # Bearer works (what cloud-push uses)
    assert "new_today" in get(f"{base}/api/summary", {"Authorization": "Bearer s3cret-token"})
    # Login form → cookie
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    req = urllib.request.Request(f"{base}/login", data=b"token=wrong", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(req)
    assert e.value.code == 401
    opener.open(urllib.request.Request(f"{base}/login", data=b"token=s3cret-token", method="POST"))
    assert "new_today" in json.load(opener.open(f"{base}/api/summary"))
    # A page from another site can't use that cookie
    with pytest.raises(urllib.error.HTTPError):
        opener.open(urllib.request.Request(f"{base}/api/summary", headers={"Origin": "https://evil.example"}))


def test_refuses_public_bind_without_token(monkeypatch):
    from pipeline.dashboard.app import run_dashboard
    monkeypatch.delenv("JOBPIPE_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        run_dashboard(host="0.0.0.0", port=0)


# ── Auto-apply ─────────────────────────────────────────────────────────

class FakeRunner:
    def __init__(self):
        self.started = []

    def busy(self):
        return False

    def start(self, args, label, auto=False, companies=None):
        self.started.append((args, label, companies))
        return {"label": label}

    def list(self, limit=20):
        return []


def test_maybe_autoapply_respects_switch_score_caps(tmp_path, monkeypatch):
    from pipeline import applicant, autoapply
    monkeypatch.setattr(applicant, "PRIVATE_DIR", tmp_path / "p")
    t = Tracker(tmp_path / "t.db")
    t.observe_board("greenhouse", "acme", [], baseline=True)
    t.observe_board("greenhouse", "acme", [_job(1, score=90), _job(2, score=85), _job(3, score=40)])
    t.observe_board("greenhouse", "beta", [], baseline=True)
    t.observe_board("greenhouse", "beta", [_job(1, "Beta", 88)])
    runner = FakeRunner()
    assert autoapply.maybe_autoapply(t, runner=runner) is None            # off by default
    autoapply.save_settings({"enabled": True, "min_score": 60, "max_per_company": 1, "batch": 5})
    autoapply.maybe_autoapply(t, runner=runner)
    args, label, companies = runner.started[0]
    assert sorted(companies) == ["Acme", "Beta"]                           # one per company, low score left out
    assert "--submit" not in args                                          # prepare mode by default
    assert args.count("-j") == 2
    autoapply.save_settings({"mode": "submit", "trust_ai": False})
    runner2 = FakeRunner()
    autoapply.maybe_autoapply(t, runner=runner2)
    assert "--submit" in runner2.started[0][0] and "--trust-ai" not in runner2.started[0][0]


def test_runner_runs_and_logs(tmp_path):
    from pipeline.autoapply import Runner
    r = Runner(tmp_path)
    meta = r.start(["--help"], "help")
    for _ in range(100):
        if r.get(meta["id"])["status"] != "running":
            break
        time.sleep(0.1)
    assert r.get(meta["id"])["status"] == "finished"
    text, offset = r.log(meta["id"])
    assert "Usage" in text and offset > 0
    assert r.get("../../etc/passwd") is None


# ── Handoff: you finish in the browser ─────────────────────────────────

def _browser_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("google-chrome") or shutil.which("chromium"))


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_handoff_captures_your_answers_and_sees_confirmation(tmp_path):
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form, wait_for_you
    form = Path(__file__).parent / "fixtures" / "forms" / "submit_flow.html"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company="Acme", title="SWE", url="", mode="practice")
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.goto(form.resolve().as_uri())
        fields = fill_form(page, {"first_name": "A", "last_name": "M"}, resume, report)
        assert [f.label for f in report.needs_attention] == ["Email*"]
        # "You" type the email and press Submit a moment later
        page.evaluate("""() => setTimeout(() => {
            document.getElementById('em').value = 'me@example.com';
            setTimeout(() => document.getElementById('f').requestSubmit(), 1500);
        }, 500)""")
        wait_for_you(page, report, fields, tmp_path, timeout_s=20)
        browser.close()
    assert report.submitted and "confirmed" in report.submit_note
    assert report.captured == {"Email*": "me@example.com"}
