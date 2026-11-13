"""Tests for application details: storage, resume parsing, and the local-only page."""

import io
import json
import shutil
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from pipeline import applicant

REPO = Path(__file__).resolve().parents[1]


def test_private_dir_is_gitignored():
    for path in ["data/private/applicant.json", "data/private/resume.pdf"]:
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path])
        assert r.returncode == 0, f"{path} is NOT gitignored"


def test_save_load_and_permissions(tmp_path):
    saved = applicant.save({"first_name": " Amudhan ", "authorized_us": "Yes", "bogus": "x"}, tmp_path)
    assert saved["first_name"] == "Amudhan" and "bogus" not in saved
    assert applicant.load(tmp_path)["authorized_us"] == "Yes"
    assert applicant.load(tmp_path)["gender"] == applicant.DECLINE   # default
    mode = stat.S_IMODE((tmp_path / "applicant.json").stat().st_mode)
    assert mode == 0o600


def test_invalid_choice_rejected(tmp_path):
    with pytest.raises(ValueError):
        applicant.save({"sponsorship_future": "Maybe"}, tmp_path)


def test_missing_required():
    missing = applicant.missing_required(applicant.defaults())
    assert "Phone" in missing and "Do you require visa sponsorship now?" in missing


def test_resume_must_be_pdf(tmp_path):
    with pytest.raises(ValueError):
        applicant.save_resume(b"hello", tmp_path)
    applicant.save_resume(b"%PDF-1.7 ...", tmp_path)
    assert applicant.has_resume(tmp_path)


class TestParse:
    def test_two_column_resume_text(self):
        # What pypdf actually extracts from templates/generic.tex
        text = ("Amudhan Muthaiah\nam15241@nyu.edu | linkedin.com/in/amudhan | github.com/amudhan23\n"
                "Education\nNew Y ork University New York, NY\n"
                "Master of Science in Computer Science Aug 2024  May 2026\n")
        p = applicant.parse_resume(text)
        assert p == {
            "first_name": "Amudhan", "last_name": "Muthaiah", "email": "am15241@nyu.edu",
            "linkedin": "https://linkedin.com/in/amudhan", "github": "https://github.com/amudhan23",
            "school": "New York University", "degree": "Master of Science",
            "major": "Computer Science", "graduation": "May 2026",
        }

    def test_university_of_and_phone(self):
        p = applicant.parse_resume(
            "Jane Doe\njane@x.com | (646) 555-0123\nUniversity of Texas at Austin  Austin, TX\n"
            "Bachelor of Science in Computer Science, Expected May 2027")
        assert p["school"] == "University of Texas at Austin"
        assert p["phone"] == "(646) 555-0123" and p["graduation"] == "May 2027"

    def test_nothing_found(self):
        assert applicant.parse_resume("") == {}


# ── The page / API ─────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path):
    from pipeline.dashboard.app import DashboardHandler
    DashboardHandler.private_dir = tmp_path / "private"
    DashboardHandler.db_path = tmp_path / "t.db"
    srv = HTTPServer(("127.0.0.1", 0), DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "private"
    srv.shutdown()
    DashboardHandler.private_dir = None


def _req(url, data=None, ctype=None, headers=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if ctype:
        req.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_profile_round_trip(server):
    base, pdir = server
    code, _, body = _req(f"{base}/api/profile", json.dumps({"phone": "646-555-0123"}).encode(),
                         "application/json")
    assert code == 200
    assert json.loads(_req(f"{base}/api/profile")[2])["values"]["phone"] == "646-555-0123"
    assert (pdir / "applicant.json").exists()


def test_no_cors_header_so_other_sites_cant_read(server):
    base, _ = server
    _, headers, _ = _req(f"{base}/api/profile")
    assert "Access-Control-Allow-Origin" not in headers


def test_foreign_host_or_origin_rejected(server):
    base, _ = server
    assert _req(f"{base}/api/profile", headers={"Host": "evil.example"})[0] == 403          # DNS rebinding
    assert _req(f"{base}/api/profile", json.dumps({}).encode(), "application/json",
                {"Origin": "https://evil.example"})[0] == 403                               # cross-site post


def test_form_style_post_refused(server):
    base, pdir = server
    code, _, _ = _req(f"{base}/api/profile", b"phone=1", "application/x-www-form-urlencoded")
    assert code == 415 and not (pdir / "applicant.json").exists()


def test_resume_upload_parses(server):
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = reportlab.Canvas(buf)
    for i, line in enumerate(["Jane Doe", "jane@example.com | (646) 555-0123",
                              "Columbia University", "Master of Science in Computer Science May 2026"]):
        c.drawString(72, 750 - 18 * i, line)
    c.save()

    base, pdir = server
    code, _, body = _req(f"{base}/api/resume", buf.getvalue(), "application/pdf")
    found = json.loads(body)["found"]
    assert code == 200 and (pdir / "resume.pdf").exists()
    assert found["email"] == "jane@example.com" and found["school"] == "Columbia University"
    assert _req(f"{base}/api/resume", b"not a pdf", "application/pdf")[0] == 400


def test_dashboard_binds_localhost_by_default():
    import inspect
    from pipeline.dashboard.app import run_dashboard
    assert inspect.signature(run_dashboard).parameters["host"].default == "127.0.0.1"


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex not installed")
def test_resume_template_builds(tmp_path):
    """Regression: '%' in bullets ('by 40%') used to comment out LaTeX and break the build."""
    from pipeline.models import ATSPlatform, Job
    from pipeline.resume.generator import ResumeGenerator
    from pipeline.utils.config import load_config
    gen = ResumeGenerator(REPO / "templates" / "generic.tex", load_config())
    job = Job(id="t1", title="Backend Engineer", company="Acme", company_slug="acme",
              location="NYC", url="u", platform=ATSPlatform.ASHBY, description="backend java")
    out = gen.build(job, tmp_path)
    assert out and out.exists()


def test_answers_api_scoped(server):
    base, pdir = server
    applicant.add_pending([{"question": "years active on the platform?", "label": "Years?",
                            "options": [], "company": "Twitch"}], pdir)
    body = {"answers": [{"question": "years active on the platform?", "company": "Twitch",
                         "answer": "0", "everywhere": False}]}
    code, _, out = _req(f"{base}/api/answers", json.dumps(body).encode(), "application/json")
    data = json.loads(out)
    assert code == 200
    assert data["saved"]["companies"] == {"twitch": {"years active on the platform?": "0"}}
    assert data["saved"]["all"] == {} and data["pending"] == {}
    assert _req(f"{base}/api/answers", json.dumps({"q": "a"}).encode(), "application/json")[0] == 400
