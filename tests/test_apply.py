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


def test_apply_command_yes_needs_submit(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline.cli import main
    _cli_env(tmp_path, monkeypatch, details=DETAILS)
    result = CliRunner().invoke(main, ["apply", "--yes"])
    assert result.exit_code != 0 and "--submit" in result.output


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


# ── Twitch-style form: the failures from the first real run ────────────

GH_NEW = Path(__file__).parent / "fixtures" / "forms" / "greenhouse_new.html"
TWITCH_DETAILS = {
    "first_name": "Amudhan", "email": "a@x.com", "country": "United States", "city": "New York, NY",
    "relocate": "Yes", "current_employer": "None", "past_employers": "IBM, Sahara AI",
    "authorized_us": "Yes", "sponsorship_now": "No", "sponsorship_future": "Yes",
    "h1b_before": "No", "citizenship": "India", "future_roles": "Yes",
}


def _fill(path, details, saved=None, company="Twitch", resume=None):
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form
    import tempfile
    if resume is None:  # forms ask for a resume; supply one unless a test opts out
        resume = Path(tempfile.mkdtemp()) / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company=company, title="SWE", url="", mode="practice")
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.set_default_timeout(8000)
        page.goto(path.resolve().as_uri())
        fill_form(page, details, resume, report, saved)
        values = page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('[role=combobox]')].map(e => [e.id, e.value]))")
        checked = page.evaluate(
            "() => [...document.querySelectorAll('input[type=checkbox]:checked')].map(e => e.value)")
        browser.close()
    return report, values, checked


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_twitch_style_form():
    report, values, _ = _fill(GH_NEW, TWITCH_DETAILS)
    labels = [f.label for f in report.filled + report.needs_attention + report.skipped_optional]
    assert len(labels) == len(set(labels)), "a question was listed twice (hidden twin input)"
    assert values == {
        "country": "United States",            # not "United States Minor Outlying Islands"
        "city": "New York, NY, USA",           # late suggestions, searched by city only
        "relocate": "New York, NY",            # options are cities, not Yes/No → your city
        "employee": "No", "amazon": "No",      # not a current employee anywhere
        "eligible": "Yes", "spons": "Yes", "h1b": "No",
        "citizen": "India", "future": "Yes",
        "exp": "",                             # company-specific: left for you
    }
    assert [f.label.split(" (")[0] for f in report.needs_attention] == \
        ["How would you describe your experience with Twitch?"]
    plat = [f for f in report.skipped_optional if f.kind == "checkboxes"]
    assert len(plat) == 1 and plat[0].options == ["TikTok", "YouTube", "None"]


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_saved_answers_close_the_loop(tmp_path):
    from pipeline import applicant
    q = applicant.normalize_question("How would you describe your experience with Twitch? (Select one)*", "Twitch")
    assert q == "how would you describe your experience with {company}?"
    applicant.add_pending([{"question": q, "label": "How would you…", "options": ["I stream"],
                            "company": "Twitch"}], tmp_path)
    saved = applicant.save_answers([
        {"question": q, "company": "Twitch", "answer": "I don't use Twitch"},
        {"question": "which other platforms are you active on?", "company": "Twitch", "answer": "YouTube, None"},
    ], tmp_path)
    assert applicant.load_pending(tmp_path) == {}            # answered → no longer pending

    report, values, checked = _fill(GH_NEW, TWITCH_DETAILS, saved)
    assert values["exp"] == "I don't use Twitch"
    assert sorted(checked) == ["none", "yt"]
    assert report.needs_attention == []


class TestAnswerScope:
    """Company-specific answers must not leak into the general profile."""

    def test_company_answer_stays_with_company(self, tmp_path):
        from pipeline import applicant
        q = "how would you describe your experience with {company}?"
        saved = applicant.save_answers([{"question": q, "company": "Twitch", "answer": "I watch streams"}], tmp_path)
        assert saved["all"] == {}
        assert answer_for("How would you describe your experience with Twitch?", {}, "Twitch", saved).value \
            == "I watch streams"
        assert answer_for("How would you describe your experience with Stripe?", {}, "Stripe", saved) is None

    def test_everywhere_answer_is_shared(self, tmp_path):
        from pipeline import applicant
        q = "have you previously applied to {company}?"
        saved = applicant.save_answers([{"question": q, "company": "Twitch", "answer": "No",
                                         "everywhere": True}], tmp_path)
        assert answer_for("Have you previously applied to Stripe?", {}, "Stripe", saved).value == "No"

    def test_company_answer_overrides_shared(self, tmp_path):
        from pipeline import applicant
        q = "have you previously applied to {company}?"
        applicant.save_answers([{"question": q, "company": "", "answer": "No", "everywhere": True}], tmp_path)
        saved = applicant.save_answers([{"question": q, "company": "Twitch", "answer": "Yes"}], tmp_path)
        assert answer_for("Have you previously applied to Twitch?", {}, "Twitch", saved).value == "Yes"
        assert answer_for("Have you previously applied to Stripe?", {}, "Stripe", saved).value == "No"

    def test_pending_is_per_company_and_cleared_by_shared_answer(self, tmp_path):
        from pipeline import applicant
        q = "do you have hands-on experience in sap?"
        for c in ("Twitch", "Acme"):
            applicant.add_pending([{"question": q, "label": "SAP?", "options": [], "company": c}], tmp_path)
        assert set(applicant.load_pending(tmp_path)) == {"Twitch", "Acme"}
        applicant.save_answers([{"question": q, "company": "Twitch", "answer": "No"}], tmp_path)
        assert set(applicant.load_pending(tmp_path)) == {"Acme"}          # only Twitch answered
        applicant.save_answers([{"question": q, "company": "Acme", "answer": "No", "everywhere": True}], tmp_path)
        assert applicant.load_pending(tmp_path) == {}

    def test_forget_answer(self, tmp_path):
        from pipeline import applicant
        q = "do you have hands-on experience in sap?"
        applicant.save_answers([{"question": q, "company": "Twitch", "answer": "No"}], tmp_path)
        saved = applicant.save_answers([{"question": q, "company": "Twitch", "answer": ""}], tmp_path)
        assert saved["companies"] == {}

    def test_old_files_migrate(self, tmp_path):
        import json as _json
        from pipeline import applicant
        (tmp_path / "answers.json").write_text(_json.dumps({"q one": "a"}))
        (tmp_path / "pending_questions.json").write_text(_json.dumps(
            {"q two": {"label": "Q two", "options": [], "companies": ["Twitch"]}}))
        assert applicant.load_answers(tmp_path) == {"all": {"q one": "a"}, "companies": {}}
        assert applicant.load_pending(tmp_path) == {"Twitch": {"q two": {"label": "Q two", "options": []}}}


def test_company_name_generalizes():
    from pipeline import applicant
    a = applicant.normalize_question("Are you currently a Twitch employee?*", "Twitch")
    b = applicant.normalize_question("Are you currently a Stripe employee?", "Stripe")
    assert a == b == "are you currently a {company} employee?"


def test_employer_rules():
    d = {"current_employer": "None", "past_employers": "IBM, Sahara AI"}
    assert answer_for("Are you currently a Twitch employee?", d, "Twitch").value == "No"
    assert answer_for("Have you previously worked for IBM?", d, "IBM").value == "Yes"
    assert answer_for("Have you ever been employed by Amazon?", d, "Twitch").value == "No"
    assert answer_for("Where are you currently employed?", d) is None       # wants a name
    assert answer_for("Are you currently a Twitch employee?", {}, "Twitch") is None  # unknown → ask


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_relocation_reports_closest_option():
    report, _, _ = _fill(GH_NEW, TWITCH_DETAILS)
    reloc = next(f for f in report.filled if f.label.startswith("Are you open to relocation"))
    assert reloc.value == "New York, NY" and "closest option" in reloc.source


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_unmatched_dropdown_does_not_block_later_questions():
    """Twitch bug: an unanswerable dropdown left its menu open over the next
    questions, so they timed out. Without a city, relocation can't match."""
    details = {k: v for k, v in TWITCH_DETAILS.items() if k != "city"}
    report, values, _ = _fill(GH_NEW, details)
    assert values["relocate"] == ""
    assert values["employee"] == "No" and values["amazon"] == "No" and values["eligible"] == "Yes"
    assert not any("TimeoutError" in f.source for f in report.needs_attention + report.skipped_optional)


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_closed_posting_detected(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pipeline.apply.filler import practice_apply

    class Gone(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404); self.end_headers(); self.wfile.write(b"Page not found")
        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), Gone)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        job = {"id": "simplify-x", "company": "Anysphere", "title": "SWE", "platform": "ashby", "url": ""}
        report = practice_apply(job, {}, None, tmp_path / "out",
                                url=f"http://127.0.0.1:{srv.server_address[1]}/cursor/x/application")
    finally:
        srv.shutdown()
    assert report.closed and "job closed" in report.error


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_greenhouse_resume_attached(tmp_path):
    """Live Twitch form: file input labelled "Attach", question on the group.
    The resume was silently skipped as an optional "Attach" field."""
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company="Twitch", title="SWE", url="", mode="practice")
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.set_default_timeout(8000)
        page.goto(GH_NEW.resolve().as_uri())
        fill_form(page, TWITCH_DETAILS, resume, report)
        files = page.eval_on_selector("#resume", "e => [...e.files].map(f => f.name)")
        cover = page.eval_on_selector("#cover_letter", "e => e.files.length")
        browser.close()
    assert files == ["resume.pdf"]
    assert report.resume_attached
    res = next(f for f in report.filled if f.kind == "file")
    assert res.label == "Resume/CV*" and res.required
    assert cover == 0                                                    # no cover letter on file
    assert [f.label for f in report.skipped_optional if f.kind == "file"] == ["Cover Letter"]
    assert not any(f.label == "Attach" for f in report.filled + report.needs_attention + report.skipped_optional)


# ── Round 3: from the 6-job run (Twitch regression, Kalshi, Engine) ─────

RS5 = Path(__file__).parent / "fixtures" / "forms" / "react_select_v5.html"
ASHBY_SHORT = Path(__file__).parent / "fixtures" / "forms" / "ashby_short.html"


def _shown_values(path, details, saved=None, company="Acme"):
    """Fill, then read what each dropdown shows (react-select keeps the
    choice in a single-value div, not the input)."""
    from playwright.sync_api import sync_playwright
    from pipeline.apply.filler import FillReport, _launch, fill_form
    import tempfile
    resume = Path(tempfile.mkdtemp()) / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    report = FillReport(job_id="t", company=company, title="SWE", url="", mode="practice")
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        page = browser.new_page()
        page.set_default_timeout(8000)
        page.goto(path.resolve().as_uri())
        fill_form(page, details, resume, report, saved)
        shown = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[role=combobox]')]
            .map(e => [e.id, e.closest('.field').querySelector('.select__single-value')?.innerText || '']))""")
        captcha = page.eval_on_selector_all("[name=g-recaptcha-response]", "els => els.map(e => e.value)")
        browser.close()
    return report, shown, captcha


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_dropdown_reads_only_its_own_list():
    """Live Twitch run: every dropdown said "no option matching" — options
    were read from the whole page, not the list the dropdown opened."""
    details = {**TWITCH_DETAILS, "sponsorship_now": "Yes"}
    report, shown, _ = _shown_values(RS5, details, saved={"all": {}, "companies": {}})
    assert shown["country"] == "United States +1"       # dial-code list; closest, not "…Islands +1"
    assert shown["spons"] == "Yes" and shown["h1b"] == "No" and shown["citizen"] == "India"
    filled = {f.label: f.value for f in report.filled}
    assert filled["Country*"] == "United States +1"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_choice_that_does_not_stick_is_not_reported_filled():
    report, shown, _ = _shown_values(RS5, TWITCH_DETAILS)
    assert shown["future"] == ""
    fut = next(f for f in report.needs_attention if f.label.startswith("Would you like to be considered"))
    assert "didn't keep it" in fut.source
    assert not report.ready_to_submit


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_unmatched_dropdown_says_what_it_offered():
    details = {**TWITCH_DETAILS, "citizenship": "Narnia"}
    report, _, _ = _shown_values(RS5, details)
    cit = next(f for f in report.needs_attention if f.label.startswith("In which country"))
    assert "no option matching 'Narnia'" in cit.source
    assert "'Canada'" in cit.source and "'India'" in cit.source


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_final_check_catches_empty_required_fields():
    """'Why do you want to work here?' has no answer → must block 'ready'."""
    report, _, _ = _shown_values(RS5, TWITCH_DETAILS)
    labels = [f.label for f in report.needs_attention]
    assert "Why do you want to work here?*" in labels
    assert len(labels) == len(set(labels))


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_ashby_short_form_skips_captcha_and_autofill():
    details = {"first_name": "Amudhan", "last_name": "Muthaiah", "email": "a@x.com"}
    report, _, captcha = _shown_values(ASHBY_SHORT, details, company="Kalshi")
    seen = report.filled + report.needs_attention + report.skipped_optional
    assert captcha == [""]                                    # never touched
    assert not any("recaptcha" in f.label.lower() for f in seen)
    files = [f for f in seen if f.kind == "file"]
    assert [f.label for f in files] == ["Resume*"]            # not the autofill uploader
    assert report.resume_attached and report.ready_to_submit


def test_redirect_to_job_list_means_closed():
    from pipeline.apply.filler import _redirected_away
    t = "https://job-boards.greenhouse.io/engine/jobs/8046374003"
    assert _redirected_away(t, "https://job-boards.greenhouse.io/engine?error=true")
    assert _redirected_away(t, "https://job-boards.greenhouse.io/engine")
    assert not _redirected_away(t, t)
    assert not _redirected_away(t, t + "?gh_src=abc")
    a = "https://jobs.ashbyhq.com/kalshi/49734bb7/application"
    assert not _redirected_away(a, a)


def test_pick_option_prefers_closest():
    opts = ["United States Minor Outlying Islands +1", "United States +1", "India +91"]
    assert pick_option("United States", opts) == "United States +1"


def test_same_application_selected_once():
    from pipeline.cli import _one_per_application
    jobs = [
        {"id": "ash-kalshi-1", "platform": "ashby", "company": "Kalshi",
         "title": "Software Engineer - Backend", "url": "https://jobs.ashbyhq.com/kalshi/1"},
        {"id": "simplify-9", "platform": "ashby", "company": "Kalshi",
         "title": "Software Engineer, Backend", "url": "https://jobs.ashbyhq.com/kalshi/1/application"},
        {"id": "ash-kalshi-2", "platform": "ashby", "company": "Kalshi",
         "title": "Data Engineer", "url": "https://jobs.ashbyhq.com/kalshi/2"},
    ]
    assert [j["id"] for j in _one_per_application(jobs)] == ["ash-kalshi-1", "ash-kalshi-2"]


def test_merge_title_key_ignores_punctuation():
    from pipeline.orchestrator import _title_key
    assert _title_key("Software Engineer - Backend") == _title_key("Software Engineer, Backend")


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_greenhouse_closed_redirect_detected(tmp_path):
    """Engine: the job URL redirects to the board with ?error=true."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pipeline.apply.filler import practice_apply

    class Board(BaseHTTPRequestHandler):
        def do_GET(self):
            if "/jobs/" in self.path:
                self.send_response(302); self.send_header("Location", "/engine?error=true"); self.end_headers()
                return
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h1>Current openings at Engine</h1><a href='/engine/jobs/2'>Other job</a>")
        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        job = {"id": "gh-engine-1", "company": "Engine", "title": "SWE", "platform": "greenhouse", "url": ""}
        report = practice_apply(job, {}, None, tmp_path / "out",
                                url=f"http://127.0.0.1:{srv.server_address[1]}/engine/jobs/1")
    finally:
        srv.shutdown()
    assert report.closed and "job closed" in report.error


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_required_checkbox_list_is_one_question():
    """Live Twitch: each box of a required list was reported separately."""
    report, _, _ = _shown_values(RS5, TWITCH_DETAILS)
    plat = [f for f in report.needs_attention if "platforms" in f.label]
    assert len(plat) == 1 and plat[0].kind == "checkboxes"
    assert plat[0].options == ["TikTok", "YouTube", "None"]
    assert not any(f.label in ("TikTok", "YouTube", "None") for f in report.needs_attention)


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_required_checkbox_list_answered_passes_check():
    saved = {"all": {}, "companies": {"acme": {
        "which other platforms are you active on?": "YouTube"}}}
    report, _, _ = _shown_values(RS5, TWITCH_DETAILS, saved=saved)
    assert not any("platforms" in f.label for f in report.needs_attention)
    assert any("platforms" in f.label and f.value == "YouTube" for f in report.filled)


def test_new_derived_answers():
    d = {"country": "United States", "citizenship": "India", "other_pr": "No"}
    assert answer_for("Are you located in the US?*", d).value == "Yes"
    assert answer_for("Do you currently hold an active U.S. security clearance?*", d).value == "No"
    # export-control status list: legal choice, left to you unless a U.S. citizen
    q = "Please select if you are a (i) U.S. citizen or national, (ii) U.S. lawful, permanent resident"
    assert answer_for(q, d) is None
    assert answer_for(q, {**d, "citizenship": "United States"}).value == "U.S. citizen or national"
    assert answer_for("Are you a permanent resident of a country other than your citizenship?", d).value == "No"
    ref = answer_for("How did you hear about us?*", {"referral_source": "Online job board"})
    assert pick_option(ref.alternates[0], ["LinkedIn", "Company careers page / website"]) \
        == "Company careers page / website"


# ── Submitting ──────────────────────────────────────────────────────────

SUBMIT = Path(__file__).parent / "fixtures" / "forms" / "submit_flow.html"


def _submit_cli(tmp_path, monkeypatch, url, args, input=None):
    import json
    from click.testing import CliRunner
    from pipeline.apply import filler
    from pipeline.cli import main
    from pipeline.tracker.db import Tracker
    pdir = _cli_env(tmp_path, monkeypatch, details=DETAILS)
    monkeypatch.setattr(filler, "application_url", lambda job: url)
    result = CliRunner().invoke(main, ["apply", "--top", "1", *args], input=input)
    assert result.exit_code == 0, result.output
    report = json.loads((pdir / "applications" / "ab-acme-1" / "report.json").read_text())
    return result.output, report, Tracker().get_job("ab-acme-1")


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_submit_confirmed_marks_applied(tmp_path, monkeypatch):
    out, report, job = _submit_cli(tmp_path, monkeypatch, SUBMIT.resolve().as_uri(), ["--submit", "--yes"])
    assert report["submitted"] is True and "confirmed" in report["submit_note"]
    assert job["status"] == "applied" and "submitted by jobpipe" in job["notes"]
    assert "Submitted 1 application" in out


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_submit_asks_first_and_no_means_no(tmp_path, monkeypatch):
    out, report, job = _submit_cli(tmp_path, monkeypatch, SUBMIT.resolve().as_uri(), ["--submit"], input="n\n")
    assert "Submit this application?" in out
    assert report["submitted"] is False and report["mode"] == "practice"
    assert job["status"] != "applied"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_incomplete_form_is_never_submitted(tmp_path, monkeypatch):
    out, report, job = _submit_cli(tmp_path, monkeypatch, FORM.resolve().as_uri(), ["--submit", "--yes"])
    assert report["submitted"] is False and report["mode"] == "practice"
    assert "needs you" in out and job["status"] != "applied"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_emailed_code_is_handed_to_you(tmp_path, monkeypatch):
    _, report, job = _submit_cli(tmp_path, monkeypatch, SUBMIT.resolve().as_uri() + "#code", ["--submit", "--yes"])
    assert report["submitted"] is False and "code" in report["submit_note"] and "--headed" in report["submit_note"]
    assert job["status"] != "applied"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_site_error_reported_not_applied(tmp_path, monkeypatch):
    _, report, job = _submit_cli(tmp_path, monkeypatch, SUBMIT.resolve().as_uri() + "#error", ["--submit", "--yes"])
    assert report["submitted"] is False and "There was an error" in report["submit_note"]
    assert job["status"] != "applied"


@pytest.mark.skipif(not _browser_available(), reason="playwright + Chrome needed")
def test_applied_job_not_offered_again(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline.cli import main
    _submit_cli(tmp_path, monkeypatch, SUBMIT.resolve().as_uri(), ["--submit", "--yes"])
    again = CliRunner().invoke(main, ["apply", "--submit", "--yes"])
    assert again.exit_code != 0 and "No matching jobs" in again.output


def test_round4_answers():
    d = {"city": "New York, NY", "pronouns": "Decline to self-identify", "referral_source": "LinkedIn"}
    st = answer_for("Which U.S. State or Canadian Province do you reside in?*", d)
    assert st.value == "New York" and st.alternates == ["NY"]
    assert answer_for("Pronouns *", d).value == "Decline to self-identify"
    assert pick_option("Decline to self-identify", ["He/him", "She/her", "Prefer not to say"]) == "Prefer not to say"
    assert answer_for("How did you first learn about Affirm as an employer? *", d).value == "LinkedIn"
    affirm = ["I have not previously been employed at Affirm", "I have previously been employed at Affirm"]
    assert pick_option("No", affirm) == affirm[0]
    assert pick_option("Yes", affirm) == affirm[1]
    assert pick_option("No", ["I have not previously been employed at Affirm"]) == \
        "I have not previously been employed at Affirm"
    # a lone statement isn't a Yes
    assert pick_option("Yes", ["I have not previously been employed at Affirm"]) is None
    assert answer_for("Where are you located?", d).value == "New York, NY"     # still the city rule
