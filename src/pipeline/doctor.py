"""`jobpipe doctor`: is every piece in place? `jobpipe selftest`: does the
whole chain work on a real, live job (without sending anything)?"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

OK, WARN, FAIL = "ok", "warn", "fail"


def _check(name: str, fn: Callable[[], tuple[str, str]]) -> dict[str, str]:
    try:
        level, msg = fn()
    except Exception as e:
        level, msg = FAIL, f"{type(e).__name__}: {str(e)[:200]}"
    return {"name": name, "level": level, "message": msg}


def checks(online: bool = False, private_dir: Path | None = None) -> list[dict[str, str]]:
    from pipeline import applicant, autoapply
    from pipeline.ai import client as ai_client
    from pipeline.notify import mailcodes
    from pipeline.notify.telegram import Telegram
    pdir = private_dir or applicant.PRIVATE_DIR
    cloud = os.environ.get("JOBPIPE_HEADLESS_ONLY") == "1"
    out: list[dict[str, str]] = []

    def python() -> tuple[str, str]:
        v = sys.version_info
        return (OK if v >= (3, 10) else FAIL), f"Python {v.major}.{v.minor}.{v.micro}"
    out.append(_check("Python", python))

    def storage() -> tuple[str, str]:
        pdir.mkdir(parents=True, exist_ok=True)
        probe = pdir / ".doctor"
        probe.write_text("x")
        probe.unlink()
        free = shutil.disk_usage(pdir).free // (1024 * 1024)
        return (OK if free > 500 else WARN), f"{pdir} is writable, {free} MB free"
    out.append(_check("Private folder", storage))

    def browser() -> tuple[str, str]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            v = b.version
            b.close()
        return OK, f"Headless Chromium {v} starts"
    out.append(_check("Browser for applying", browser))

    def latex() -> tuple[str, str]:
        if not shutil.which("pdflatex"):
            return WARN, "pdflatex missing: resumes can't be tailored (your uploaded resume is used)"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t.tex").write_text(r"\documentclass{article}\usepackage{enumitem,titlesec,tabularx}"
                                             r"\usepackage[hidelinks]{hyperref}\begin{document}ok\end{document}")
            r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "t.tex"], cwd=tmp,
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                return FAIL, "pdflatex is installed but the resume packages are missing (texlive-latex-extra)"
        return OK, "pdflatex builds resumes"
    out.append(_check("Resume builder", latex))

    def details() -> tuple[str, str]:
        missing = applicant.missing_required(applicant.load(pdir))
        return (FAIL, f"Missing on My details: {', '.join(missing)}") if missing else (OK, "All required fields filled")
    out.append(_check("Your details", details))

    def resume() -> tuple[str, str]:
        return (OK, "Uploaded") if applicant.has_resume(pdir) else (FAIL, "Upload a resume PDF on My details")
    out.append(_check("Resume PDF", resume))

    def profile() -> tuple[str, str]:
        import re
        from pipeline.resume.writer import has_profile, load_profile
        if not has_profile(pdir):
            return WARN, "No career profile: resumes reuse the bullets in config (Career profile page)"
        left = len(re.findall(r"\[confirm", load_profile(pdir), re.I))
        return (WARN, f"{left} [confirm] notes still to check") if left else (OK, "Saved, nothing left to confirm")
    out.append(_check("Career profile", profile))

    def ai() -> tuple[str, str]:
        ok, why = ai_client.status(pdir)
        if not ok:
            return WARN, f"AI off ({why}): no fit checks, AI answers or written resumes"
        if online:
            try:
                ai_client.complete("Reply with the single word OK.", "Say OK.", purpose="doctor key check",
                                   max_tokens=5, private_dir=pdir, model="haiku")
            except Exception as e:
                return FAIL, f"The key didn't work: {str(e)[:150]}"
        return OK, "Key saved" + (" and working" if online else "")
    out.append(_check("Anthropic API", ai))

    def telegram() -> tuple[str, str]:
        tg = Telegram(pdir)
        if not tg.configured():
            return WARN, "Not connected: no phone alerts or approvals (`jobpipe telegram setup`)"
        if online:
            me = tg.api("getMe")
            return OK, f"Connected to @{me.get('username', '?')}"
        return OK, "Connected"
    out.append(_check("Telegram", telegram))

    def mail() -> tuple[str, str]:
        if not mailcodes.configured(pdir):
            return WARN, "Not set: security-code emails will be asked for on Telegram"
        if online:
            ok, msg = mailcodes.test_login(pdir)
            return (OK if ok else FAIL), msg
        return OK, f"Reads codes from {mailcodes.settings(pdir)['email']}"
    out.append(_check("Security-code email", mail))

    def hirebase_() -> tuple[str, str]:
        from pipeline import hirebase
        if not hirebase.get_key(pdir):
            return WARN, "No key: jobs beyond your boards aren't searched (`jobpipe hirebase key`, free)"
        st = hirebase.public(pdir)
        if online:
            import httpx
            r = httpx.post(hirebase.ESTIMATE, json={"job_titles": ["Software Engineer"], "days_ago": 1},
                           headers={"x-api-key": hirebase.get_key(pdir)}, timeout=20)
            if r.status_code == 401:
                return FAIL, "Hirebase rejected the key"
        return OK, f"Key saved; {st['used']} of {st['monthly_jobs']} jobs used this month"
    out.append(_check("Hirebase", hirebase_))

    def boards() -> tuple[str, str]:
        from pipeline.discovery.registry import BoardRegistry
        n = BoardRegistry().frontier_stats().get("boards", 0)
        return (OK if n >= 20 else WARN), f"{n} job boards being watched" + (
            "" if n >= 20 else ": add companies on Search companies, or let discovery run")
    out.append(_check("Job boards", boards))

    def auto() -> tuple[str, str]:
        s = autoapply.load_settings(pdir)
        if not s["enabled"]:
            return WARN, "Auto-apply is off (Auto-apply page)"
        if autoapply.needs_review(s, pdir):
            return OK, (f"On, submit mode: approving {autoapply.approvals(pdir)}/{s['review_first']} on your phone, "
                        "then automatic")
        return OK, f"On, {s['mode']} mode, up to {s['max_per_day']} a day"
    out.append(_check("Auto-apply", auto))

    if cloud:
        def token() -> tuple[str, str]:
            t = os.environ.get("JOBPIPE_DASHBOARD_TOKEN", "")
            return (OK, "Set") if len(t) >= 24 else (FAIL, "JOBPIPE_DASHBOARD_TOKEN must be set (24+ characters)")
        out.append(_check("Dashboard password", token))

    if online:
        def network() -> tuple[str, str]:
            import httpx
            hosts = {"Greenhouse": "https://boards-api.greenhouse.io/v1/boards/discord/jobs",
                     "Lever": "https://api.lever.co/v0/postings/plaid?mode=json&limit=1",
                     "Ashby": "https://api.ashbyhq.com/posting-api/job-board/ramp"}
            bad = []
            with httpx.Client(timeout=20, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (jobpipe doctor)"}) as c:
                for name, url in hosts.items():
                    try:
                        code = c.get(url).status_code
                        if code >= 500 or code in (403, 429):
                            bad.append(f"{name} (HTTP {code})")
                    except httpx.HTTPError as e:
                        bad.append(f"{name} ({type(e).__name__})")
            if not bad:
                return OK, "Greenhouse, Lever and Ashby reachable"
            return (FAIL if len(bad) == len(hosts) else WARN), f"Trouble reaching {', '.join(bad)}"
        out.append(_check("Job sites", network))
    return out


# ── Self-test: the whole chain on a live posting, nothing sent ─────────

TEST_BOARDS = ["discord", "figma", "airbnb", "robinhood", "dropbox", "coinbase", "stripe", "gitlab"]

TEST_APPLICANT = {
    "first_name": "Jordan", "last_name": "Tester", "email": "jordan.tester@example.com",
    "phone": "(555) 010-0199", "city": "New York, NY", "country": "United States",
    "linkedin": "https://www.linkedin.com/in/example", "github": "https://github.com/example",
    "authorized_us": "Yes", "sponsorship_now": "No", "sponsorship_future": "Yes", "visa_status": "F-1 OPT",
    "school": "Example University", "degree": "Master's", "major": "Computer Science",
    "graduation": "May 2026", "start_date": "Immediately", "relocate": "Yes",
    "referral_source": "Company website", "pronouns": "Decline to self-identify",
    "gender": "Decline to self-identify", "race": "Decline to self-identify",
    "veteran": "Decline to self-identify", "disability": "Decline to self-identify",
}


def _blank_pdf(path: Path) -> Path:
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(612, 792)
    with path.open("wb") as f:
        w.write(f)
    return path


def selftest(out_dir: Path, fake: bool = False, board: str | None = None, url: str | None = None,
             say: Callable[[str], None] = print) -> dict[str, Any]:
    """Fetch a live Greenhouse job, then fill its real form in headless
    Chromium without submitting. Returns a summary; ok=False on failure."""
    import httpx
    from pipeline import applicant
    from pipeline.apply.filler import practice_apply
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"ok": False, "steps": []}

    def step(name: str, ok: bool, detail: str) -> None:
        result["steps"].append({"step": name, "ok": ok, "detail": detail})
        say(f"{'✓' if ok else '✗'} {name}: {detail}")

    t0 = time.monotonic()
    job = None
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "jobpipe-selftest"}) as c:
        for slug in ([board] if board else TEST_BOARDS):
            try:
                r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
                items = r.json().get("jobs", []) if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                items = []
            if items:
                item = next((i for i in items if "engineer" in i.get("title", "").lower()), items[0])
                job = {"id": f"gh-{slug}-{item['id']}", "title": item.get("title", ""),
                       "company": item.get("company_name") or slug, "company_slug": slug,
                       "location": (item.get("location") or {}).get("name", ""),
                       "url": item.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{item['id']}",
                       "platform": "greenhouse", "description": item.get("content", "") or "",
                       "fit_score": 0, "status": "new"}
                break
    if url and job:
        job["url"] = url
    if not job:
        step("Fetch a live job", False, "no Greenhouse board answered")
        return result
    job.setdefault("id", f"selftest-{int(time.time())}")
    job.setdefault("fit_score", 0)
    step("Fetch a live job", True, f"{job.get('company')}: {job.get('title')} ({time.monotonic() - t0:.1f}s)")

    if fake:
        details, saved = dict(TEST_APPLICANT), {"all": {}, "companies": {}}
        resume = _blank_pdf(out_dir / "Jordan_Tester_Resume.pdf")
    else:
        details, saved = applicant.load(), applicant.load_answers()
        resume = applicant.PRIVATE_DIR / applicant.RESUME_FILE
        if not resume.exists():
            step("Your resume", False, "upload a resume first (or run with --fake)")
            return result

    if not fake and shutil.which("pdflatex"):
        try:
            from pipeline.resume.tailor import tailored_resume
            pdf, note = tailored_resume(job, {}, details, out_dir / "resume")
            step("Tailored resume", bool(pdf), note[:150] if note else "built")
            resume = pdf or resume
        except Exception as e:
            step("Tailored resume", False, str(e)[:150])

    t1 = time.monotonic()
    report = practice_apply(job, details, resume, out_dir, headless=True, saved=saved,
                            on_filled=lambda rep: False)          # never submits
    if report.error:
        step("Fill the real form", False, report.error[:200])
        result["screenshot"] = str(report.screenshot or "")
        return result
    n = len([f for f in report.filled if f.value])
    step("Fill the real form", n >= 3,
         f"{n} fields filled, {len(report.needs_attention)} need you, resume "
         f"{'attached' if report.resume_attached else 'NOT attached'} ({time.monotonic() - t1:.1f}s)")
    step("Nothing was sent", not report.submitted, "practice mode: the Submit button was never pressed")
    result.update({"ok": n >= 3 and not report.submitted, "screenshot": str(report.screenshot or ""),
                   "job": {"company": job.get("company"), "title": job.get("title"), "url": job.get("url")},
                   "filled": n, "needs": [f.label for f in report.needs_attention]})
    return result
