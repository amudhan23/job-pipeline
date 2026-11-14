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
    assert "<career_profile>" in fake.calls[0]["system"][1]["text"]


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
