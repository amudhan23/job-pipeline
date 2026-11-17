"""Running unattended: phone approvals, Gmail codes, watchdog, exclusions,
repost guard, cloud sync, health endpoint, setup check."""

import email.utils
import imaplib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from pipeline.models import ATSPlatform, Job
from pipeline.tracker.db import Tracker
from tests.test_autonomy import CHAT, FakeTelegram, pdir, tg  # noqa: F401  (fixtures)


def _job(i="1", company="Acme", title="Software Engineer", score=90):
    j = Job(id=f"gh-{company.lower()}-{i}", title=title, company=company, company_slug=company.lower(),
            location="NYC", url=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{i}",
            platform=ATSPlatform.GREENHOUSE)
    j.fit_score = score
    return j


def _tracker(tmp_path, jobs):
    t = Tracker(tmp_path / "t.db")
    t.observe_board("greenhouse", "acme", [], baseline=True)
    t.observe_board("greenhouse", "acme", jobs)
    return t


def _runner(started):
    return SimpleNamespace(busy=lambda: False, list=lambda limit=20: [],
                           start=lambda args, label, auto=False, companies=None:
                           started.append((args, label)) or {"label": label})


# ── Approve the first N on your phone ──────────────────────────────────

def test_submit_mode_prepares_for_your_ok_until_n_approvals(pdir, tmp_path):  # noqa: F811
    from pipeline import autoapply
    from pipeline.apply.filler import FieldResult, FillReport
    t = _tracker(tmp_path, [_job("1"), _job("2", title="Backend Engineer")])
    autoapply.save_settings({"enabled": True, "mode": "submit", "review_first": 2, "fit_check": False})
    started = []
    autoapply.maybe_autoapply(t, runner=_runner(started))
    args, label = started[0]
    assert "--submit" not in args and "--notify-ready" in args and label.startswith("Prepared for your OK")

    # A prepared, complete application waits for the tap instead of being redone
    r = FillReport(job_id="gh-acme-1", company="Acme", title="SWE", url="", mode="practice")
    r.filled = [FieldResult("First name", "text", True, "Amudhan", "details"),
                FieldResult("Why Acme?", "textarea", True, "I built payment rails.", "AI draft")]
    r.ai_written = ["Why Acme?"]
    r.save(pdir / "applications" / "gh-acme-1")
    assert "gh-acme-1" not in [c["id"] for c in autoapply.candidates(t, autoapply.load_settings())]

    # Tap Submit: Claude's answer is kept as yours, the submit is queued, the count goes up
    assert autoapply.approve(t.get_job("gh-acme-1")) == 1
    assert autoapply.approve(t.get_job("gh-acme-1")) == 1          # same job twice counts once
    from pipeline import applicant
    acme = next(v for k, v in applicant.load_answers()["companies"].items() if "acme" in k)
    assert "I built payment rails." in acme.values()
    started.clear()
    autoapply.maybe_autoapply(t, runner=_runner(started))
    assert "--submit" in started[0][0] and "phone" in started[0][1]

    autoapply.approve(t.get_job("gh-acme-2"))
    assert not autoapply.needs_review(autoapply.load_settings())    # 2 of 2: on its own now


def test_ready_message_lists_every_answer_and_approve_button_acts(tg, pdir, tmp_path):  # noqa: F811
    from pipeline.apply.filler import FieldResult, FillReport
    t, fake = tg
    t.connect("123:ABC")
    fake.add(message={"message_id": 1, "chat": CHAT, "text": "/start"})
    t.wait_for_start(5)
    r = FillReport(job_id="gh-acme-1", company="Acme", title="SWE", url="", mode="practice")
    r.filled = [FieldResult("Email", "text", True, "a@b.co", "details"),
                FieldResult("Why Acme?", "textarea", True, "Payments infra.", "AI draft")]
    r.ai_written = ["Why Acme?"]
    t.notify_ready({"id": "gh-acme-1", "company": "Acme", "title": "SWE", "fit_score": 88}, r, progress=(3, 15))
    msg = fake.sent[-1]
    assert "Email: a@b.co" in msg["text"] and "Claude wrote this" in msg["text"] and "Approval 4 of 15" in msg["text"]
    cb = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert cb.startswith("r:")
    acted = []
    fake.add(callback_query={"id": "c1", "data": cb, "message": {"message_id": 9, "chat": CHAT}})
    t.poll_once(lambda job_id, action: acted.append((job_id, action)) or "ok")
    assert acted == [("gh-acme-1", "approve")]


# ── Never twice, never these companies ─────────────────────────────────

def test_exclusions_and_repost_guard(pdir, tmp_path):  # noqa: F811
    from pipeline import autoapply
    from pipeline.models import ApplicationStatus
    t = _tracker(tmp_path, [_job("1"), _job("2", title="Software Engineer "), _job("3", title="Data Engineer")])
    t.update_status("gh-acme-1", ApplicationStatus.APPLIED, notes="sent")
    s = autoapply.save_settings({"never_companies": "IBM\nGlobex", "fit_check": False})
    assert s["never_companies"] == ["Globex", "IBM"]
    ids = [c["id"] for c in autoapply.candidates(t, s)]
    assert ids == ["gh-acme-3"]                 # #2 is the same role re-posted
    s = autoapply.save_settings({"never_companies": ["Acme Inc."]})
    assert autoapply.candidates(t, s) == []


# ── Security codes from Gmail ──────────────────────────────────────────

GH_BODY = ("Hi Amudhan,\r\n\r\nCopy and paste this code into the security code field on your application:\r\n\r\n"
           "Xk7pQ2mZ\r\n\r\nAfter you enter the code, resubmit your application.\r\n")


def _mail(sender, subject, body, when):
    m = email.message.EmailMessage()
    m["From"], m["Subject"], m["Date"] = sender, subject, email.utils.format_datetime(when)
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages, fail_login=False):
        self.messages, self.fail_login, self.selected = messages, fail_login, None
        self.flags_changed = False

    def login(self, user, pw):
        if self.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, box, readonly=False):
        self.selected = (box, readonly)
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, mid, what):
        assert "PEEK" in what                          # never marks mail read
        raw = self.messages[int(mid) - 1]
        if "HEADER.FIELDS" in what:
            raw = raw.split(b"\n\n")[0] + b"\n\n"
        return "OK", [(b"1", raw)]

    def logout(self):
        pass


def test_code_extraction():
    from pipeline.notify.mailcodes import extract_code
    assert extract_code(GH_BODY) == "Xk7pQ2mZ"
    assert extract_code("Your verification code is 482913. It expires in 10 minutes.") == "482913"
    assert extract_code("Thanks for applying! We'll review your application soon.") is None


def test_reads_only_new_job_site_mail(pdir):  # noqa: F811
    from pipeline.notify import mailcodes
    mailcodes.save({"email": "me@gmail.com", "app_password": "abcd efgh ijkl mnop"})
    assert mailcodes.configured()
    assert "abcdefghijklmnop" not in json.dumps(mailcodes.public())
    now = datetime.now(timezone.utc)
    msgs = [_mail("Greenhouse <no-reply@us.greenhouse-mail.io>", "Security code for your application",
                  GH_BODY.replace("Xk7pQ2mZ", "OLDCODE1"), now - timedelta(hours=2)),
            _mail("Friend <pal@example.com>", "lunch code 123456?", "code 123456", now),
            _mail("Greenhouse <no-reply@us.greenhouse-mail.io>", "Security code for your application", GH_BODY, now)]
    fake = FakeIMAP(msgs)
    code = mailcodes.find_code("Acme", since=now - timedelta(minutes=1), timeout_s=1, connect=lambda h: fake,
                               sleep=lambda s: None)
    assert code == "Xk7pQ2mZ" and fake.selected == ("INBOX", True)
    # Only an old code around: wait, then give up
    old = FakeIMAP(msgs[:1])
    assert mailcodes.find_code("Acme", since=now, timeout_s=0, connect=lambda h: old, sleep=lambda s: None) is None
    # Wrong password: stop at once
    bad = FakeIMAP(msgs, fail_login=True)
    assert mailcodes.find_code("Acme", since=now, timeout_s=60, connect=lambda h: bad, sleep=lambda s: None) is None
    with pytest.raises(ValueError):
        mailcodes.save({"app_password": "short"})


# ── Health ─────────────────────────────────────────────────────────────

def test_health_alerts_once_then_says_fixed(pdir):  # noqa: F811
    from pipeline import health
    from pipeline.crawler.worker import iso
    stale = {"updated_at": iso(datetime.now(timezone.utc) - timedelta(hours=1)),
             "last_hour": {"reads": 40, "outcomes": {"error": 30, "read": 10}},
             "errors": [{"error": "403 Forbidden"}]}
    found = health.problems(status=stale)
    assert {f["key"] for f in found} >= {"crawler_stalled", "fetch_errors"}
    sent = []
    tgx = SimpleNamespace(configured=lambda: True, send=lambda text: sent.append(text) or 1)
    now = datetime.now()
    assert set(health.alert(found, tgx, now=now)) >= {"crawler_stalled", "fetch_errors"}
    assert health.alert(found, tgx, now=now + timedelta(hours=1)) == []          # not again so soon
    assert health.alert(found, tgx, now=now + timedelta(hours=7))                # still broken: reminder
    health.alert([], tgx, now=now + timedelta(hours=8))
    assert any(s.startswith("✅ Fixed") for s in sent)


# ── Cloud sync ─────────────────────────────────────────────────────────

class FakeRegistry:
    def __init__(self, rows=()):
        self.rows, self.added = list(rows), []

    def _conn(self):
        rows = self.rows

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql):
                return SimpleNamespace(fetchall=lambda: rows)
        return C()

    def add(self, platform, slug, source):
        self.added.append((platform, slug))
        return True


def test_sync_round_trip_and_refusals(pdir, tmp_path):  # noqa: F811
    from pipeline import applicant, sync
    applicant.save({"first_name": "Amudhan", "last_name": "M", "email": "a@b.co"})
    (pdir / "career_profile.md").write_text("# Profile")
    (pdir / "anthropic_key").write_text("sk-ant-secret")
    (pdir / "runs").mkdir()
    bundle, names = sync.build(registry=FakeRegistry([("greenhouse", "stripe"), ("lever", "plaid")]))
    assert "applicant.json" in names and "anthropic_key" in names and any("2 boards" in n for n in names)
    no_keys, names2 = sync.build(with_secrets=False, registry=FakeRegistry())
    assert "anthropic_key" not in names2
    target = tmp_path / "cloud"
    reg = FakeRegistry()
    out = sync.apply(bundle, target, registry=reg)
    assert (target / "career_profile.md").read_text() == "# Profile"
    assert oct((target / "anthropic_key").stat().st_mode)[-3:] == "600"
    assert reg.added == [("greenhouse", "stripe"), ("lever", "plaid")] and out["boards_added"] == 2
    for evil in ("../../etc/passwd", "runs/x.log", "applications/a/report.json"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(evil, "x")
        with pytest.raises(ValueError):
            sync.apply(buf.getvalue(), target, registry=reg)
    with pytest.raises(ValueError):
        sync.apply(b"not a zip", target, registry=reg)


# ── Dashboard: health endpoint, sync upload, what was sent ─────────────

def test_dashboard_healthz_sync_and_sent(pdir, tmp_path, monkeypatch):  # noqa: F811
    import threading
    from http.server import ThreadingHTTPServer
    from pipeline import sync
    from pipeline.apply.filler import FieldResult, FillReport
    from pipeline.dashboard import app as dash
    t = _tracker(tmp_path, [_job("1")])
    r = FillReport(job_id="gh-acme-1", company="Acme", title="SWE", url="", mode="practice")
    r.filled = [FieldResult("Why Acme?", "textarea", True, "Payments.", "AI draft")]
    r.ai_written = ["Why Acme?"]
    r.save(pdir / "applications" / "gh-acme-1")
    monkeypatch.setenv("JOBPIPE_DASHBOARD_TOKEN", "t" * 32)
    monkeypatch.setattr(sync, "apply", lambda data, private_dir=None, registry=None: {"written": ["x"], "boards_added": 0})
    dash.DashboardHandler.db_path = t.db_path if hasattr(t, "db_path") else str(tmp_path / "t.db")
    monkeypatch.setattr(dash.DashboardHandler, "_pdir", lambda self: pdir, raising=False)
    monkeypatch.setattr(dash.DashboardHandler, "_tracker", lambda self: t, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        h = httpx.get(base + "/healthz")
        assert h.status_code == 200 and h.json()["booting"] is True              # no login needed
        assert httpx.get(base + "/api/sent/gh-acme-1").status_code == 401        # but everything else does
        auth = {"Authorization": "Bearer " + "t" * 32}
        sent = httpx.get(base + "/api/sent/gh-acme-1", headers=auth).json()
        assert sent["answers"] == [{"label": "Why Acme?", "value": "Payments.", "kind": "textarea", "ai": True}]
        up = httpx.post(base + "/api/sync", content=b"PK..", headers={**auth, "Content-Type": "application/zip"})
        assert up.status_code == 200 and up.json()["written"] == ["x"]
        assert httpx.post(base + "/api/sync", content=b"x", headers={**auth, "Content-Type": "text/plain"}).status_code == 415
    finally:
        srv.shutdown()


# ── Setup check ────────────────────────────────────────────────────────

def test_doctor_reports_what_to_fix(pdir, monkeypatch):  # noqa: F811
    from pipeline import doctor
    monkeypatch.setenv("JOBPIPE_HEADLESS_ONLY", "1")
    monkeypatch.delenv("JOBPIPE_DASHBOARD_TOKEN", raising=False)
    found = {c["name"]: c for c in doctor.checks(private_dir=pdir)}
    assert found["Your details"]["level"] == "fail" and "Missing" in found["Your details"]["message"]
    assert found["Resume PDF"]["level"] == "fail"
    assert found["Dashboard password"]["level"] == "fail"
    assert found["Python"]["level"] == "ok"
    assert all(c["level"] in ("ok", "warn", "fail") for c in found.values())


def test_rehearse_reports_setup_and_stops_on_failures(pdir):  # noqa: F811
    from click.testing import CliRunner
    from pipeline.cli import main
    r = CliRunner().invoke(main, ["rehearse", "--skip", "apply,telegram,mail,leads"])
    assert "Setup check" in r.output and "Your details" in r.output
    assert r.exit_code == 1 and "to fix before the cloud" in r.output
