"""The profile-based resume writer: every line checked against the profile."""

import json
import shutil

import pytest

from pipeline.ai import client as ai
from pipeline.resume import writer as w
from tests.test_ai import KEY, FakeClient

PROFILE = """# Career profile: Test Person

## Rules
- Total experience is **3.5 years**.
- IBM was **Java / Spring Boot**. Never list Go as an IBM language.
- No em dashes in resume bullets.
- Never claim: Kubernetes in production [confirm], AWS [confirm], GraphQL, CUDA work you don't own.

## Experience

### IBM: Software Engineer
Bangalore, India | Jan 2021 – Jul 2024
Stack: Java, Spring Boot, PostgreSQL, Redis, Jenkins, Azure
- Led the migration of a **50,000-line** ColdFusion codebase to Spring Boot microservices with zero downtime, cutting maintenance cost **30%**.
- Tuned PostgreSQL queries and added **Redis** caching, cutting API latency **40%**.
- Cloud: Azure. [confirm: some resumes said AWS/Kubernetes at IBM]

### Sahara AI: Software Engineer Intern
Los Angeles, CA | Jun 2025 – Aug 2025
Stack: Solidity, TypeScript, Viem, LayerZero
- Built ERC-4337 account abstraction on Base mainnet with custom Paymaster contracts.

### Indian Statistical Institute: Research Intern
Kolkata | 2019 – 2020
Stack: Go
- Wrote a Go simulator for consensus protocols.

## Projects
- **req-guard** | Rust, Tokio, Hyper | github.com/test/req-guard
  Rate limiter handling 50K requests per second.
- **GPU physics engine** | C++, CUDA
  CUDA kernels for cloth simulation.

## Skills by level
- **Used in production:** Java, Spring Boot, PostgreSQL, Redis, Solidity, TypeScript
- **Used in projects:** Rust, Go, C++, CUDA
- **Familiar only:** Terraform

## Education
- **New York University**: MS Computer Science, 2024 – 2026.
"""

GOOD_AND_BAD = {
    "experience": [
        {"company": "IBM", "title": "Software Engineer", "location": "Bangalore, India",
         "dates": "Jan 2021 – Jul 2024",
         "bullets": [
             "Led migration of a **50,000-line** ColdFusion codebase to **Spring Boot** microservices, cutting maintenance cost **30%**.",
             "Cut API latency **40%** with **Redis** caching and PostgreSQL tuning.",
             "Built Go services handling 2M requests a day.",            # Go not at IBM; 2M invented
             "Deployed services on **Kubernetes** and AWS.",            # only in a [confirm] note + banned
             "Reduced costs **55%** across the platform.",              # invented number
         ]},
        {"company": "Sahara AI", "title": "Software Engineer Intern", "location": "Los Angeles, CA",
         "dates": "Jun 2025 – Aug 2025",
         "bullets": ["Built **ERC-4337** account abstraction with custom Paymaster contracts on Base mainnet.",
                     "Built a GraphQL gateway for the chain APIs."]},  # banned + not there
        {"company": "Google", "title": "SWE", "dates": "2020", "bullets": ["Did things."]},
    ],
    "projects": [
        {"name": "req-guard", "stack": "Rust, Tokio, Hyper", "link": "github.com/test/req-guard",
         "bullets": ["Rate limiter in **Rust** handling **50K** requests per second.",
                     "Handles 90K requests per second."]},
        {"name": "GPU physics engine", "stack": "C++, CUDA",
         "bullets": ["Wrote **CUDA** kernels for cloth simulation."]},     # CUDA allowed: in its own entry
        {"name": "Moon Lander", "bullets": ["Landed on the moon."]},
    ],
    "skills": [{"label": "Languages", "items": "Java, Go, Rust, C++, Kotlin"},
               {"label": "Infra", "items": "Redis, Kubernetes, Terraform, Azure"}],
    "education": [{"school": "New York University", "degree": "MS Computer Science", "dates": "2024 – 2026"}],
    "note": "Backend role; led with IBM.",
}


def _why(dropped, needle):
    return [d for d in dropped if needle in d["line"]]


def test_never_claim_terms_and_notes_dont_count():
    assert {"Kubernetes", "AWS", "GraphQL", "CUDA"} <= set(w.never_claim(PROFILE))
    ibm = [v for k, v in w.sections(PROFILE).items() if k.startswith("IBM")][0]
    assert "AWS" not in ibm and "Kubernetes" not in ibm      # only mentioned inside a [confirm] note


def test_check_keeps_supported_lines_and_drops_the_rest():
    clean, dropped = w.check(json.loads(json.dumps(GOOD_AND_BAD)), PROFILE)
    ibm = clean["experience"][0]["bullets"]
    assert len(ibm) == 2 and "50,000-line" in ibm[0]
    assert _why(dropped, "Go services") and _why(dropped, "Kubernetes") and _why(dropped, "55%")
    assert "never claim" in _why(dropped, "Kubernetes")[0]["why"]
    assert [b for b in clean["experience"][1]["bullets"] if "ERC-4337" in b]
    assert _why(dropped, "GraphQL")
    assert "Google" not in [e["company"] for e in clean["experience"]]
    projects = {p["name"]: p["bullets"] for p in clean["projects"]}
    assert projects["req-guard"] == ["Rate limiter in **Rust** handling **50K** requests per second."]
    assert projects["GPU physics engine"]                       # CUDA is in that project's own entry
    assert "Moon Lander" not in projects
    skills = {s["label"]: s["items"] for s in clean["skills"]}
    assert "Kotlin" not in skills["Languages"] and "Go" in skills["Languages"]
    assert "Kubernetes" not in skills["Infra"] and "Terraform" not in skills["Infra"]
    assert "Redis" in skills["Infra"]


def test_tex_escape_and_bold():
    assert w.tex_escape("cut **40%** & more_x") == r"cut \textbf{40\%} \& more\_x"
    assert "—" not in w.tex_escape("a — b")


def test_profile_saved_private(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    assert w.load_profile(tmp_path) == PROFILE and w.has_profile(tmp_path)
    assert oct((tmp_path / w.PROFILE_FILE).stat().st_mode)[-3:] == "600"


def test_profile_reaches_other_ai_features(tmp_path):
    from pipeline.ai.context import applicant_block
    assert "career_profile" not in applicant_block({}, "resume", tmp_path)
    w.save_profile(PROFILE, tmp_path)
    assert "Never claim" in applicant_block({}, "resume", tmp_path)


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_write_resume_builds_one_page(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    fake = FakeClient(text=json.dumps(GOOD_AND_BAD))
    details = {"first_name": "Test", "last_name": "Person", "email": "t@example.com",
               "github": "github.com/test"}
    job = {"company": "Acme", "title": "Backend Engineer", "description": "Java, Spring Boot, Redis."}
    pdf, info = w.write_resume(job, details, tmp_path / "out", "Resume.pdf", tmp_path, fake)
    assert pdf and pdf.exists() and w._pages(pdf) == 1
    tex = pdf.with_suffix(".tex").read_text()
    assert "Kubernetes" not in tex and "Moon Lander" not in tex and r"\textbf{50,000-line}" in tex
    assert info["dropped"]
    assert info["parse_problems"] == [] and "parses as text" in info["steps"]
    assert "<career_profile>" in fake.calls[0]["system"][0]["text"]       # first, so it's cached across calls
    assert fake.calls[0]["system"][0].get("cache_control")


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_tailored_resume_prefers_profile(tmp_path):
    from pipeline.resume.tailor import tailored_resume
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    fake = FakeClient(text=json.dumps(GOOD_AND_BAD))
    job = {"company": "Acme", "title": "Backend Engineer", "description": "Java and Redis."}
    pdf, note = tailored_resume(job, {}, {"first_name": "T", "last_name": "P"}, tmp_path / "job",
                                private_dir=tmp_path, client=fake)
    assert pdf and "Dropped" in note
    meta = json.loads((tmp_path / "job" / "tailor.json").read_text())
    assert meta["by"] == "profile" and meta["dropped"]


def test_find_matches_short_and_long_institution_names():
    prof = ("### NYU Courant Institute (New York University): Research Affiliate, zero-knowledge proofs\n"
            "- Implemented **Bulletproofs** in **Rust**.\n\n### IBM: Software Engineer\n- Owned **Java** services.\n")
    secs = w.sections(prof)
    assert "Research Affiliate" in (w._find(secs, "New York University, Courant Institute", "role") or "")
    assert "Research Affiliate" in (w._find(secs, "NYU", "role") or "")
    assert "IBM" in (w._find(secs, "IBM India", "role") or "")
    assert w._find(secs, "Google", "role") is None


def test_generic_words_are_not_claims():
    ok, why = w.supported("Built an async **HTTP** reverse proxy in **Rust** with clean **APIs**", "reverse proxy in Rust")
    assert ok, why
    assert not w.supported("Deployed on **Kubernetes**", "reverse proxy in Rust")[0]


def test_no_bullet_bank_resume_when_the_writer_fails(tmp_path):
    """With a career profile, a failed write is an error, not a generic resume."""
    from pipeline.resume.tailor import tailored_resume
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    fake = FakeClient(text="sorry, no JSON here")
    job = {"company": "Acme", "title": "Backend Engineer", "description": "Java and Redis."}
    pdf, note = tailored_resume(job, {}, {"first_name": "T", "last_name": "P"}, tmp_path / "job",
                                private_dir=tmp_path, client=fake)
    assert pdf is None and "resume writer failed" in note
    assert len(fake.calls) == 4                       # plan + write, retried once
    assert fake.calls[0].get("thinking", {}).get("type") == "enabled"
    assert not list((tmp_path / "job").glob("*.pdf")) if (tmp_path / "job").exists() else True


def test_thinking_falls_back_when_the_model_lacks_it(tmp_path):
    ai.save_key(KEY, tmp_path)

    class NoThinking(FakeClient):
        def create(self, **kw):
            if "thinking" in kw:
                raise RuntimeError("thinking is not supported for this model")
            return super().create(**kw)
    fake = NoThinking(text="ok")
    r = ai.complete("sys", "hi", purpose="t", max_tokens=5000, private_dir=tmp_path, client=fake, thinking=2000)
    assert r.text == "ok" and "thinking" not in fake.calls[-1]


def test_resume_request_asks_for_guaranteed_json(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    fake = FakeClient(text=json.dumps(GOOD_AND_BAD))
    w.draft({"company": "Acme", "title": "Backend", "description": "Java"}, PROFILE, tmp_path, fake)
    fmt = fake.calls[0]["extra_body"]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


def test_broken_json_is_repaired_once(tmp_path):
    ai.save_key(KEY, tmp_path)
    broken = '{"experience": [{"company": "IBM", "bullets": ["Built the "fast" path"]}]}'
    fake = FakeClient(text='{"experience": [{"company": "IBM", "bullets": ["Built the fast path"]}]}')
    out = w.parse_json(broken, tmp_path, fake)
    assert out["experience"][0]["company"] == "IBM" and len(fake.calls) == 1


def test_schema_dropped_when_the_api_rejects_it(tmp_path):
    ai.save_key(KEY, tmp_path)

    class NoSchema(FakeClient):
        def create(self, **kw):
            if "extra_body" in kw:
                raise RuntimeError("output_config: unknown field")
            return super().create(**kw)
    fake = NoSchema(text="{}")
    assert ai.complete("s", "p", purpose="t", private_dir=tmp_path, client=fake, schema={"type": "object"}).text == "{}"


def test_resumes_use_opus_by_default_and_never_haiku(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    fake = FakeClient(text=json.dumps(GOOD_AND_BAD))
    w.draft({"company": "A", "title": "B", "description": "Java"}, PROFILE, tmp_path, fake)
    assert fake.calls[-1]["model"] == ai.MODELS["opus"]
    ai.save_settings({"model": "haiku", "resume_model": "sonnet"}, tmp_path)
    w.draft({"company": "A", "title": "B", "description": "Java"}, PROFILE, tmp_path, fake)
    assert fake.calls[-1]["model"] == ai.MODELS["sonnet"]


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_trimming_never_guts_the_main_job(tmp_path):
    long = "Built and shipped a service with careful attention to many details across teams " * 2
    resume = {"experience": [
        {"company": "NYU", "title": "Research", "location": "NY", "dates": "2026", "bullets": [long] * 2},
        {"company": "Sahara", "title": "Intern", "location": "Remote", "dates": "2025", "bullets": [long] * 3},
        {"company": "IBM", "title": "Engineer", "location": "India", "dates": "2021", "bullets": [long] * 6}],
        "projects": [{"name": f"P{i}", "stack": "Rust", "link": "", "context": "", "bullets": [long] * 3}
                     for i in range(5)],
        "skills": [{"label": "L", "items": "Rust"}] * 5, "education": [],
        "extras": [{"label": "Awards", "text": "x"}, {"label": "Publications", "text": "y"}]}
    pdf, trimmed = w.fit_one_page(resume, {"first_name": "T"}, tmp_path / "r.pdf")
    assert pdf and w._pages(pdf) == 1
    ibm = resume["experience"][2]
    assert len(ibm["bullets"]) >= 3
    # a denser layout now holds this much: extras stay unless the page is still over
    assert len(trimmed) >= 1


def test_reference_resume_is_style_only(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    (tmp_path / w.REFERENCE_FILE).write_bytes(b"%PDF fake")
    import pipeline.applicant as appl
    orig = appl.resume_text
    appl.resume_text = lambda b: "REFERENCE STYLE TEXT"
    try:
        fake = FakeClient(text=json.dumps(GOOD_AND_BAD))
        w.draft({"company": "A", "title": "B", "description": "Java"}, PROFILE, tmp_path, fake)
    finally:
        appl.resume_text = orig
    prompt = fake.calls[-1]["messages"][0]["content"]
    assert "REFERENCE STYLE TEXT" in prompt and "NOT a source of facts" in prompt



class Scripted(FakeClient):
    """Answers each call with the next reply in a list."""
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def create(self, **kw):
        self.text = self.replies.pop(0)
        return super().create(**kw)


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_plan_then_write_then_review(tmp_path):
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    the_plan = {"job_summary": "Backend for payments", "requirements": [
        {"need": "Java services in production", "must": True, "evidence": "IBM Spring Boot", "strength": "strong"}],
        "keywords": ["Spring Boot"], "experience": [], "projects": [], "skills": [], "extras": [],
        "leave_out": ["blockchain projects"]}
    reviewed = json.loads(json.dumps(GOOD_AND_BAD))
    reviewed["note"] = "REVIEWED"
    fake = Scripted([json.dumps(the_plan), json.dumps(GOOD_AND_BAD), json.dumps(reviewed)])
    job = {"company": "Acme", "title": "Backend Engineer", "description": "Java, Spring Boot, Redis. " * 30}
    pdf, info = w.write_resume(job, {"first_name": "T"}, tmp_path / "out", "Resume.pdf", tmp_path, fake)
    assert pdf and info["steps"] == ["planned", "written", "reviewed", "parses as text"]
    assert info["note"] == "REVIEWED" and info["plan"]["requirements"][0]["strength"] == "strong"
    assert "Backend for payments" in fake.calls[1]["messages"][0]["content"]          # the plan reached the writer
    assert "<draft>" in fake.calls[2]["messages"][0]["content"]                      # the reviewer saw the draft
    assert fake.calls[0]["extra_body"]["output_config"]["format"]["schema"]["required"][0] == "job_summary"
    assert all(c["system"][0]["text"] == fake.calls[0]["system"][0]["text"] for c in fake.calls)   # cache reuse


def test_missing_description_is_fetched(monkeypatch):
    from pipeline.ai import context
    import httpx

    class R:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><script>x()</script><main>" + "Build agents that run for hours. " * 20 + "</main></html>"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    job = context.ensure_description({"title": "Founding Engineer", "url": "https://jobs.example.com/1"})
    assert "agents that run for hours" in job["description"] and "x()" not in job["description"]
    full = {"description": "word " * 100, "url": "https://x"}
    assert context.ensure_description(full) is full


def test_job_from_url_builds_a_job_for_any_posting(monkeypatch, tmp_path):
    """`jobpipe resume --url` works for postings the crawler never saw: the
    page gives the description and, failing better, the company and title."""
    import httpx
    from pipeline.ai import context

    class R:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = ("<html><head><title>Founding Engineer, Agents - Clera</title></head><body>"
                + "You will build agents that run for hours. " * 20 + "</body></html>")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    job = context.job_from_url("https://jobs.ashbyhq.com/clera/abc")
    assert job["id"].startswith("adhoc_") and job["source"] == "adhoc"
    assert job["title"] == "Founding Engineer, Agents" and job["company"] == "Clera"
    assert "agents that run for hours" in job["description"]
    # given names win, a pasted description wins over the page
    job = context.job_from_url("https://x.example/1", company="Acme", title="SWE", description="pasted " * 80)
    assert (job["company"], job["title"]) == ("Acme", "SWE") and job["description"].startswith("pasted")

    # a posting the crawler saved is used as-is (its id keeps the folder stable)
    from pipeline.tracker.db import Tracker
    t = Tracker(tmp_path / "jobs.db")
    class FakeTracker:
        def get_job_by_url(self, url):
            return {"id": "gh_1", "url": url, "company": "Saved", "title": "Kept", "description": "d " * 70}
    job = context.job_from_url("https://x.example/2", tracker=FakeTracker())
    assert job["id"] == "gh_1" and job["company"] == "Saved"
    assert t.get_job_by_url("https://nowhere") is None


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_pdf_reads_back_as_clean_text(tmp_path):
    """What a screening system extracts: real fonts, no tables, no ligature
    glyphs, no words split by hyphenation — every bullet comes back whole."""
    resume = {"experience": [{"title": "Software Engineer", "company": "IBM", "location": "Bengaluru", "dates": "2021 -- 2024",
                              "bullets": ["Cut cloud spend 28% by **profiling** hot paths; shipped behind a feature flag with zero incidents " * 2,
                                          "Mentored two new hires through their first production changes, offline and office-wide"]}],
              "projects": [{"name": "Sahara", "stack": "Go, Postgres, Redis, Docker, Kubernetes, AWS", "link": "https://github.com/x/sahara",
                            "context": "Multi-tenant payments ledger with idempotent transfers",
                            "bullets": ["Implemented a double-entry ledger with idempotency keys and optimistic locking"]}],
              "skills": [{"label": "Languages", "items": "Java, Python, Go"}]}
    details = {"first_name": "Amudhan", "last_name": "M", "email": "a@example.com", "phone": "+1 646 555 0100"}
    pdf, trimmed = w.fit_one_page(resume, details, tmp_path / "r.pdf")
    assert pdf and not trimmed
    text = w.pdf_text(pdf)
    assert "profiling" in text and "feature flag" in text and "first production" in text and "\ufb01" not in text
    assert w.ats_check(pdf, resume, details) == []
    tex = pdf.with_suffix(".tex").read_text()
    assert "tabular" not in tex and "lmodern" in tex and "cmap" in tex and "11pt" in tex   # short resume: roomier type
    assert "github.com/x/sahara" in tex and "Multi-tenant payments ledger" not in tex     # context dropped, not cut
    # a resume that overflows at 11pt is set at 10pt before anything is trimmed
    resume["experience"][0]["bullets"] = ["Did a thing that mattered to the team and to the customers " * 3] * 18
    pdf, trimmed = w.fit_one_page(resume, details, tmp_path / "r2.pdf")
    assert pdf and w._pages(pdf) == 1 and "10pt" in pdf.with_suffix(".tex").read_text()


def test_first_real_build_lessons():
    """From the first resume written with the real API (Nov 24)."""
    # 'PMs' is people, not a technology claim: the lead IBM bullet must not be dropped for it
    assert w.supported("Owned **3 backend services** with PMs and designers", "Owned 3 backend services with product managers")[0]
    # bold is capped at three terms per bullet
    assert w._cap_bold("Cut **API** times **40%** in **Java** with **Redis**") == "Cut **API** times **40%** in **Java** with Redis"
    # LaTeX's curly apostrophe doesn't fail the parse check
    assert w._plain("Built the team’s stack – fast") == "built the team's stack - fast"


def test_description_falls_back_to_a_rendered_page(monkeypatch):
    """startup.jobs answered 403 to a plain request in the live run; sites
    that refuse or draw the posting with JavaScript get a browser render."""
    import httpx
    from pipeline.ai import context

    class R:
        status_code = 403
        headers = {"content-type": "text/html"}
        text = "<html>Forbidden</html>"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    monkeypatch.setattr(context, "_rendered_text", lambda url: "Build the orchestration engine " * 30)
    job = context.ensure_description({"url": "https://startup.jobs/x", "description": ""})
    assert job["description"].startswith("Build the orchestration engine")
    monkeypatch.setattr(context, "_rendered_text", lambda url: "")
    assert context.ensure_description({"url": "https://startup.jobs/x", "description": ""})["description"] == ""


def test_board_api_is_asked_first_for_ashby_lever_greenhouse_postings(monkeypatch):
    """Ashby pages are drawn with JavaScript and a plain request sees nothing;
    the board's public API has the posting as clean text."""
    import httpx
    from pipeline.ai import context
    calls = []

    class R:
        def __init__(self, data):
            self.status_code, self._d, self.headers, self.text = 200, data, {"content-type": "application/json"}, ""

        def json(self):
            return self._d
    def fake_get(url, **k):
        calls.append(url)
        if "api.ashbyhq.com" in url:
            return R({"jobs": [{"id": "1D4AEB39-2A3D-4B5A-B8C4-F9E3963004F9", "title": "Founding Engineer, Agents",
                                "location": "New York", "jobUrl": "https://jobs.ashbyhq.com/clera/1d4aeb39", "descriptionPlain": "Own the agent layer. " * 40}]})
        if "api.lever.co" in url:
            return R({"text": "Backend Engineer", "categories": {"location": "Remote"}, "descriptionPlain": "Go services. " * 40})
        if "boards-api.greenhouse.io" in url:
            return R({"title": "SWE", "location": {"name": "SF"}, "content": "&lt;p&gt;Build things.&lt;/p&gt; " * 40})
        raise AssertionError(url)
    monkeypatch.setattr(httpx, "get", fake_get)
    j = context.ensure_description({"url": "https://jobs.ashbyhq.com/Clera/1d4aeb39-2a3d-4b5a-b8c4-f9e3963004f9", "title": ""})
    assert j["description"].startswith("Own the agent layer") and j["title"] == "Founding Engineer, Agents" and j["location"] == "New York"
    # a board URL plus a title picks the live posting (search engines index postings long gone)
    j = context.ensure_description({"url": "https://jobs.ashbyhq.com/clera", "title": "founding engineer"})
    assert j["title"] == "Founding Engineer, Agents" and j["description"].startswith("Own the agent layer")
    j = context.ensure_description({"url": "https://jobs.lever.co/acme/5f3c2a10-1234-4abc-9def-000000000001"})
    assert j["description"].startswith("Go services") and j["title"] == "Backend Engineer"
    j = context.ensure_description({"url": "https://job-boards.greenhouse.io/acme/jobs/4012345"})
    assert "Build things." in j["description"] and "<p>" not in j["description"]
    assert all("api" in c for c in calls)        # never fell through to the page


def test_trim_takes_the_main_jobs_fifth_bullet_before_a_projects_second(monkeypatch):
    from pathlib import Path
    """Third real build: the two lead projects were cut to one bullet each
    while the main job kept six."""
    resume = {"experience": [{"title": "SE", "company": "IBM", "dates": "2021", "bullets": [f"IBM {i}" for i in range(6)]},
                             {"title": "Intern", "company": "S", "dates": "2025", "bullets": ["S 0", "S 1"]}],
              "projects": [{"name": "P1", "bullets": ["P1 0", "P1 1"]}, {"name": "P2", "bullets": ["P2 0", "P2 1"]}],
              "skills": [], "extras": []}
    pages = iter([2, 2, 2, 2, 1])        # 11pt, 10pt, then three trims until it fits
    monkeypatch.setattr(w, "build_pdf", lambda *a, **k: Path("/tmp/x.pdf"))
    monkeypatch.setattr(w, "_pages", lambda p: next(pages))
    _, trimmed = w.fit_one_page(resume, {}, Path("/tmp/x.pdf"))
    assert trimmed == ["IBM 5", "IBM 4", "P2 1"]
    assert all(len(p["bullets"]) >= 1 for p in resume["projects"]) and len(resume["experience"][0]["bullets"]) == 4


def test_experience_renders_newest_first_whatever_the_writer_sent():
    """A real build led with IBM (2021-24) over the current research role:
    recruiters read reverse-chronological, emphasis belongs in the bullets."""
    resume = {"experience": [
        {"title": "Software Engineer", "company": "IBM", "dates": "Jan 2021 -- Jul 2024", "bullets": ["a"]},
        {"title": "Intern", "company": "Sahara", "dates": "Jun 2025 -- Aug 2025 (11-week internship)", "bullets": ["b"]},
        {"title": "Blockchain Developer", "company": "Tokenizer", "dates": "Dec 2019 -- Jun 2020", "bullets": ["d"]},
        {"title": "Research Affiliate", "company": "NYU", "dates": "Jul 2026 -- Present", "bullets": ["c"]}]}
    tex = w.render(resume, {"first_name": "A", "last_name": "B"})
    assert tex.index("Research Affiliate") < tex.index("Intern") < tex.index("Software Engineer") < tex.index("Blockchain Developer")
    # two project links render as repo · live
    tex = w.render({"projects": [{"name": "Commentator", "stack": "TypeScript", "bullets": ["x"],
                                  "link": "https://github.com/a/commentator | https://commentator.onrender.com"}]},
                   {"first_name": "A", "last_name": "B"})
    assert "{repo}" in tex and "{live}" in tex and "commentator.onrender.com" in tex


def test_rules_persist_and_bind_every_build(tmp_path):
    """The chat got better because corrections stayed in its context; here
    they stay in resume_rules.md and go into every plan/write/review call."""
    w.save_profile(PROFILE, tmp_path)
    ai.save_key(KEY, tmp_path)
    assert w.load_rules(tmp_path) == ""
    rules = w.add_rule("Never mention Angular", tmp_path)
    rules = w.add_rule("  Lead with  req-guard for Rust roles ", tmp_path)
    assert rules.count("\n") == 2 and "Never mention Angular" in rules and "Lead with req-guard" in rules
    sysb = w._system(PROFILE, "instr", tmp_path)
    assert sysb[0]["text"].startswith("<career_profile>") and "<applicant_rules>" in sysb[1]["text"] \
        and "Never mention Angular" in sysb[1]["text"] and sysb[2]["text"] == "instr"
    assert w.add_rule("   ", tmp_path) == rules                      # blank adds nothing
    w.save_rules("", tmp_path)
    assert len(w._system(PROFILE, "instr", tmp_path)) == 2            # no rules, no block


def test_writer_can_set_header_location_projects_title_and_role_links():
    """From the applicant's rules: SF header for Bay Area roles, 'AI Agent
    Projects' as the section title, repo links on the research role's line,
    [repo] [live] then the event on a project's right side."""
    resume = {"header_location": "San Francisco, CA", "projects_title": "AI Agent Projects",
              "experience": [{"title": "Research Affiliate", "company": "NYU Courant", "dates": "Jul 2026 -- Present",
                              "links": "https://github.com/a/bulletProofRust https://github.com/a/golden-dkg", "bullets": ["x"]}],
              "projects": [{"name": "IntentGuard", "stack": "Rust", "context": "Raingentic Hackathon 2026, solo",
                            "link": "https://github.com/a/intentguard https://intentguard.live", "bullets": ["y"]}]}
    tex = w.render(resume, {"first_name": "A", "last_name": "B", "resume_location": "New York, NY"})
    assert "San Francisco, CA" in tex and "New York, NY" not in tex
    assert r"\section{AI Agent Projects}" in tex
    assert "{bulletProofRust}" in tex and "{golden-dkg}" in tex
    assert tex.index("{repo}") < tex.index("{live}") < tex.index("Raingentic Hackathon 2026, solo")
    # the check keeps those fields
    clean, _ = w.check({**resume, "skills": [], "education": [], "extras": []}, PROFILE)
    assert clean["header_location"] == "San Francisco, CA" and clean["projects_title"] == "AI Agent Projects"


def test_orphan_words_get_tightened_without_new_facts(tmp_path):
    """A bullet ending with "40%." alone on its last line is shortened by the
    model; a rewrite that adds a number or drops a tool is refused."""
    ai.save_key(KEY, tmp_path)
    orphan = ("Built the team's observability stack from scratch (distributed tracing, metrics, alerting), "
              "cutting production incidents 40%.")
    resume = {"experience": [{"company": "IBM", "title": "Engineer", "location": "India", "dates": "Jan 2021 - Jul 2024",
                              "bullets": [orphan, "Tuned PostgreSQL queries and added Redis caching, p95 down 60%."]}],
              "projects": [], "skills": [{"label": "Languages", "items": "Java"}], "education": [], "extras": []}
    pdf, _ = w.fit_one_page(resume, {"first_name": "T", "last_name": "U"}, tmp_path / "r.pdf")
    found = w.orphans(pdf)
    assert found and found[0][1].startswith("40%")
    good = "Built the team's observability stack (distributed tracing, metrics, alerting), cutting incidents 40%."
    bad = "Built the observability stack with Datadog, cutting production incidents 45%."
    fake = FakeClient(text=json.dumps({"bullets": [bad]}))
    pdf2, fixed = w.fix_orphans(pdf, resume, {"first_name": "T", "last_name": "U"}, tmp_path / "r.pdf", tmp_path, fake)
    assert fixed == [] and resume["experience"][0]["bullets"][0] == orphan     # new tool + new number: refused
    fake = FakeClient(text=json.dumps({"bullets": [good]}))
    pdf2, fixed = w.fix_orphans(pdf, resume, {"first_name": "T", "last_name": "U"}, tmp_path / "r.pdf", tmp_path, fake)
    assert fixed == [orphan] and resume["experience"][0]["bullets"][0] == good
    assert not w.orphans(pdf2)
