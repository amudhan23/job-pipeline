"""Resume crafting, company search, Telegram, retries, code relay."""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pipeline.models import ATSPlatform, Job
from pipeline.tracker.db import Tracker

KEY = "sk-ant-api03-" + "x" * 40


class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0) if self.replies else "{}"
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    from pipeline import applicant
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", d)
    return d


def _config():
    import yaml
    return yaml.safe_load((Path(__file__).parents[1] / "config" / "profile.yaml").read_text())


# ── Resume crafting ────────────────────────────────────────────────────

@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex needed")
def test_reworded_resume_keeps_facts(pdir, tmp_path):
    from pipeline.ai import client as ai
    from pipeline.applicant import resume_text
    from pipeline.resume import tailor
    ai.save_key(KEY, pdir)
    arrange = json.dumps({"ibm": ["db", "own"], "sahara": ["int"], "projects": ["req-guard"],
                          "languages": ["Java"], "note": "Backend role."})
    reword = json.dumps({"ibm": {
        "db": "Cut API response times by 40% by tuning PostgreSQL and MySQL query plans and adding Redis caching",
        "own": "Owned 3 backend services and REST APIs on AWS processing 10K+ daily requests using Java and Spring Boot",
        "dbg": "Drove root-cause debugging across 12 layers cutting production incidents by 40%"}})
    fake = FakeClaude([arrange, reword])
    job = {"id": "gh-acme-1", "company": "Acme", "title": "Backend Engineer", "description": "Postgres latency",
           "platform": "greenhouse", "url": "u"}
    folder = tmp_path / "app"
    pdf, note = tailor.tailored_resume(job, _config(), {"first_name": "A", "last_name": "M"}, folder,
                                       private_dir=pdir, client=fake)
    text = " ".join(resume_text(pdf.read_bytes()).split())
    assert "Cut API response times by 40%" in text                 # kept: same facts, JD-first
    assert "AWS" not in text                                        # rejected: adds a technology
    assert "12 layers" not in text                                  # rejected: new number
    info = json.loads((folder / "tailor.json").read_text())
    kept = {c["key"]: c for c in info["changes"]}
    assert kept["db"]["kept"] and not kept["own"]["kept"] and "AWS" in kept["own"]["why"]
    assert "new number" in kept["dbg"]["why"]
    assert "Reworded 1 bullet" in note


def test_faithful_guard():
    from pipeline.resume.tailor import faithful
    o = "Tuned PostgreSQL and MySQL query plans and introduced Redis caching reducing API response times by 40%"
    assert faithful(o, "Cut API response times by 40% tuning PostgreSQL and MySQL plans with Redis caching")[0]
    assert not faithful(o, "Cut API response times by 60% tuning PostgreSQL and MySQL plans with Redis caching")[0]
    assert not faithful(o, "Tuned PostgreSQL query plans with Redis caching, cutting API response times 40%")[0]  # drops MySQL
    assert not faithful(o, o + " across a 20-person team serving millions of users daily on Kubernetes")[0]


# ── Search ─────────────────────────────────────────────────────────────

class FakeBoards:
    """Greenhouse has 'janestreet'; nothing else exists."""
    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/boards/janestreet/jobs":
            return httpx.Response(200, json={"jobs": [
                {"id": 7, "title": "Software Engineer", "location": {"name": "New York, NY"},
                 "absolute_url": "https://x/7", "updated_at": "2026-11-14T09:00:00Z"},
                {"id": 8, "title": "Quantitative Trader", "location": {"name": "New York, NY"},
                 "absolute_url": "https://x/8", "updated_at": "2026-11-14T09:00:00Z"},
                {"id": 9, "title": "FPGA Engineer", "location": {"name": "London"},
                 "absolute_url": "https://x/9", "updated_at": "2026-11-14T09:00:00Z"}]})
        if p.startswith("/v1/boards/janestreet/jobs/"):
            i = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": int(i), "title": {"7": "Software Engineer", "9": "FPGA Engineer"}[i],
                                             "location": {"name": {"7": "New York, NY", "9": "London"}[i]},
                                             "content": "OCaml", "absolute_url": f"https://x/{i}"})
        return httpx.Response(404)


def test_search_finds_portal_and_reads_it_with_your_roles(pdir, tmp_path):
    from pipeline.crawler.fetcher import Fetcher
    from pipeline.discovery.registry import BoardRegistry
    from pipeline.orchestrator import Pipeline
    from pipeline import search as srch
    cfg = _config()
    pipe = Pipeline(cfg, registry=BoardRegistry(tmp_path / "r.db"))
    pipe.tracker = Tracker(tmp_path / "t.db")
    fake = FakeBoards()
    fetcher = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)), min_gap=0)
    s = srch.upsert_search("Jane Street", "software engineer, fpga engineer", anywhere=True, watch=True)
    assert "janestreet" in srch.slug_candidates("Jane Street")
    out = asyncio.run(srch.run_search(s, cfg, pipeline=pipe, fetcher=fetcher, web=False))
    r = out["results"]["Jane Street"]
    assert r["boards"] == [["greenhouse", "janestreet"]]
    assert sorted(r["jobs"]) == ["gh-janestreet-7", "gh-janestreet-9"]      # anywhere → London kept; trader out
    j = pipe.tracker.get_job("gh-janestreet-7")
    assert j["company"] == "Jane Street" and j["fit_score"] > 0 and j["baseline"] == 1
    # Watchlist: the crawler reads this board with the search's roles plus the profile's
    over = srch.watch_overrides(cfg)
    roles = over[("greenhouse", "janestreet")]["role_keywords"]
    assert "fpga engineer" in roles and "software engineer" in roles
    assert srch.load_searches()[0]["last_run"]


def test_unknown_company_says_why(pdir, tmp_path):
    from pipeline.crawler.fetcher import Fetcher
    from pipeline.discovery.registry import BoardRegistry
    from pipeline.orchestrator import Pipeline
    from pipeline import search as srch
    cfg = _config()
    pipe = Pipeline(cfg, registry=BoardRegistry(tmp_path / "r.db"))
    pipe.tracker = Tracker(tmp_path / "t.db")
    fetcher = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), min_gap=0)
    s = srch.upsert_search("Nowhere Corp", "")
    out = asyncio.run(srch.run_search(s, cfg, pipeline=pipe, fetcher=fetcher, web=False))
    assert "couldn't find" in out["results"]["Nowhere Corp"]["error"]


# ── Telegram ───────────────────────────────────────────────────────────

class FakeTelegram:
    def __init__(self):
        self.sent, self.updates, self.answered = [], [], []
        self.next_id = 100

    def handler(self, req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[1]
        body = json.loads(req.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "jobpipe_test_bot"}})
        if method == "getUpdates":
            off = body.get("offset", 0)
            ups = [u for u in self.updates if u["update_id"] >= off] if off >= 0 else self.updates[-1:]
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({**body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "answerCallbackQuery":
            self.answered.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": False, "description": "nope"})

    def add(self, **kw):
        self.updates.append({"update_id": len(self.updates) + 1, **kw})


CHAT = {"id": 555, "type": "private"}


@pytest.fixture
def tg(pdir):
    from pipeline.notify.telegram import Telegram
    fake = FakeTelegram()
    t = Telegram(pdir, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return t, fake


def test_setup_then_alert_with_buttons(tg):
    t, fake = tg
    assert t.connect("123:ABC") == "jobpipe_test_bot"
    fake.add(message={"message_id": 1, "chat": CHAT, "text": "/start"})
    assert t.wait_for_start(5) == 555 and t.configured()
    sent = t.notify_new([{"id": "gh-acme-1", "company": "Acme", "title": "SWE", "fit_score": 82, "url": "https://x"},
                         {"id": "gh-acme-2", "company": "Acme", "title": "Low", "fit_score": 30}])
    assert sent == 1
    msg = fake.sent[-1]
    assert "Acme" in msg["text"] and msg["reply_markup"]["inline_keyboard"][0][0]["text"] == "Submit it"


def test_buttons_and_replies_act(tg, pdir):
    from pipeline import applicant, autoapply
    t, fake = tg
    t.connect("123:ABC")
    fake.add(message={"message_id": 1, "chat": CHAT, "text": "/start"})
    t.wait_for_start(5)
    t.notify_new([{"id": "gh-acme-1", "company": "Acme", "title": "SWE", "fit_score": 82}])
    cb = fake.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    t.notify_questions({"id": "gh-acme-1", "company": "Acme"}, [
        {"company": "Acme", "question": "do you know sap?", "label": "Do you know SAP?*", "options": ["Yes", "No"]},
        {"company": "Acme", "question": "why {company}?", "label": "Why Acme?", "options": []}])
    qcb = fake.sent[-2]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]   # "No"
    reply_to = fake.sent[-1]["message_id"]
    acted = []
    fake.add(callback_query={"id": "c1", "data": cb, "message": {"message_id": 9, "chat": CHAT}})
    fake.add(callback_query={"id": "c2", "data": qcb, "message": {"message_id": 10, "chat": CHAT}})
    fake.add(message={"message_id": 11, "chat": CHAT, "text": "Because payments infra.",
                      "reply_to_message": {"message_id": reply_to}})
    fake.add(message={"message_id": 12, "chat": {"id": 999, "type": "private"}, "text": "/pause"})  # a stranger
    fake.add(message={"message_id": 13, "chat": CHAT, "text": "/pause"})
    t.poll_once(lambda job_id, action: acted.append((job_id, action)) or "ok")
    assert acted == [("gh-acme-1", "submit")]
    saved = applicant.load_answers()["companies"]
    acme = next(v for k, v in saved.items() if "acme" in k)
    assert acme["do you know sap?"] == "No" and acme["why {company}?"] == "Because payments infra."
    assert autoapply.load_settings()["enabled"] is False
    # questions aren't sent twice
    before = len(fake.sent)
    t.notify_questions({"id": "gh-acme-1", "company": "Acme"}, [
        {"company": "Acme", "question": "do you know sap?", "label": "Do you know SAP?*", "options": ["Yes", "No"]}])
    assert len(fake.sent) == before


def test_ask_waits_for_your_reply(tg):
    t, fake = tg
    t.connect("123:ABC")
    fake.add(message={"message_id": 1, "chat": CHAT, "text": "/start"})
    t.wait_for_start(5)
    orig = fake.handler

    def handler(req):
        resp = orig(req)
        if req.url.path.endswith("sendMessage") and "security code" in json.loads(req.content)["text"]:
            fake.add(message={"message_id": 50, "chat": CHAT, "text": "ABCD1234",
                              "reply_to_message": {"message_id": fake.next_id}})
        return resp
    t.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert t.ask("Acme emailed a security code", timeout_s=10, crawler_polls=False) == "ABCD1234"


# ── Retry once questions are answered ──────────────────────────────────

def test_retry_after_answer(pdir, tmp_path):
    from pipeline import applicant, autoapply
    from pipeline.apply.filler import FieldResult, FillReport
    t = Tracker(tmp_path / "t.db")
    j = Job(id="gh-acme-1", title="Software Engineer", company="Acme", company_slug="acme", location="NYC",
            url="https://job-boards.greenhouse.io/acme/jobs/1", platform=ATSPlatform.GREENHOUSE)
    j.fit_score = 90
    t.observe_board("greenhouse", "acme", [], baseline=True)
    t.observe_board("greenhouse", "acme", [j])
    r = FillReport(job_id=j.id, company="Acme", title="SWE", url="", mode="submit")
    r.needs_attention = [FieldResult("Why Acme?", "textarea", True, "", "no saved answer for this question")]
    r.save(pdir / "applications" / j.id)
    s = autoapply.load_settings()
    assert autoapply.candidates(t, s) == []
    applicant.save_answers([{"question": applicant.normalize_question("Why Acme?", "Acme"),
                             "answer": "Payments.", "company": "Acme", "everywhere": False}])
    assert [c["id"] for c in autoapply.candidates(t, s)] == [j.id]
    for _ in range(3):
        autoapply.count_attempt(j.id)
    assert autoapply.candidates(t, s) == []                  # gives up after 3 tries


def test_phone_queue_runs_first_even_with_auto_off(pdir, tmp_path):
    from pipeline import autoapply
    t = Tracker(tmp_path / "t.db")
    j = Job(id="gh-acme-1", title="SWE", company="Acme", company_slug="acme", location="NYC",
            url="https://x", platform=ATSPlatform.GREENHOUSE)
    t.upsert_jobs([j])
    started = []
    runner = SimpleNamespace(busy=lambda: False, list=lambda limit=20: [],
                             start=lambda args, label, auto=False, companies=None: started.append((args, label)) or {"label": label})
    autoapply.enqueue("gh-acme-1", "submit")
    autoapply.maybe_autoapply(t, runner=runner, allow_auto=False)
    assert "--submit" in started[0][0] and "phone" in started[0][1]
    assert autoapply.maybe_autoapply(t, runner=runner) is None   # queue drained; auto off


# ── Security code relay (unattended submit) ────────────────────────────

def _browser_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("google-chrome") or shutil.which("chromium"))


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_emailed_code_typed_in_from_your_reply(tmp_path):
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form, submit_application
    form = Path(__file__).parent / "fixtures" / "forms" / "submit_flow.html"
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    report = FillReport(job_id="t", company="Acme", title="SWE", url="", mode="practice")
    asked = []
    with sync_playwright() as pw:
        b = _launch(pw, headless=True)
        page = b.new_page()
        page.goto(form.resolve().as_uri() + "#code")
        fill_form(page, {"first_name": "A", "last_name": "M", "email": "a@x.com"}, resume, report)
        submit_application(page, report, tmp_path, get_code=lambda m: asked.append(m) or "ABCD1234", wait_seconds=20)
        b.close()
    assert asked and "security code" in asked[0]
    assert report.submitted, report.submit_note
