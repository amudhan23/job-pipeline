"""Your application details — the answers every job form asks for.

Stored only on this machine in data/private/ (gitignored, never committed,
file mode 600). When auto-apply runs in the cloud, they'll be pushed up
encrypted with your state key — GitHub will only ever hold ciphertext.

Fill it in on the dashboard's "My details" page; uploading your resume
pre-fills what can be read from it.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from pipeline.state import DATA_DIR

PRIVATE_DIR = DATA_DIR / "private"
PROFILE_FILE = "applicant.json"
RESUME_FILE = "resume.pdf"
MAX_RESUME_BYTES = 10 * 1024 * 1024

YES_NO = ["", "Yes", "No"]
DECLINE = "Decline to self-identify"

# (key, label, kind, options, default, help) — drives the form and validation.
# kind: text | email | tel | url | date | choice | textarea
FIELDS: list[dict[str, Any]] = [
    {"section": "Contact"},
    {"key": "first_name", "label": "First name", "kind": "text"},
    {"key": "last_name", "label": "Last name", "kind": "text"},
    {"key": "email", "label": "Email", "kind": "email"},
    {"key": "phone", "label": "Phone", "kind": "tel"},
    {"key": "city", "label": "City, State", "kind": "text", "help": "e.g. New York, NY"},
    {"key": "country", "label": "Country you live in", "kind": "text", "default": "United States"},
    {"key": "linkedin", "label": "LinkedIn URL", "kind": "url"},
    {"key": "github", "label": "GitHub URL", "kind": "url"},
    {"key": "website", "label": "Portfolio / website", "kind": "url"},

    {"section": "Work authorization"},
    {"key": "authorized_us", "label": "Are you legally authorized to work in the US?",
     "kind": "choice", "options": YES_NO, "help": "On F-1 OPT this is usually Yes"},
    {"key": "sponsorship_now", "label": "Do you require visa sponsorship now?",
     "kind": "choice", "options": YES_NO},
    {"key": "sponsorship_future", "label": "Will you require sponsorship in the future (e.g. H-1B)?",
     "kind": "choice", "options": YES_NO},
    {"key": "visa_status", "label": "Current visa / work status", "kind": "text",
     "help": "e.g. F-1 OPT (STEM OPT eligible)"},
    {"key": "citizenship", "label": "Country of citizenship", "kind": "text", "help": "e.g. India"},
    {"key": "h1b_before", "label": "Have you ever held H-1B status or had an H-1B petition approved?",
     "kind": "choice", "options": YES_NO},
    {"key": "other_pr", "label": "Are you a permanent resident of a country other than your citizenship?",
     "kind": "choice", "options": YES_NO},

    {"section": "Common screening questions"},
    {"key": "current_employer", "label": "Current employer", "kind": "text",
     "help": "Write None if you're not employed — answers \"Are you a current <company> employee?\""},
    {"key": "past_employers", "label": "Past employers (comma-separated)", "kind": "text",
     "help": "e.g. IBM, Sahara AI — answers \"Have you previously worked for <company>?\""},
    {"key": "noncompete", "label": "Are you bound by a non-compete or similar agreement?",
     "kind": "choice", "options": YES_NO},
    {"key": "future_roles", "label": "Want to be considered for other roles at the company?",
     "kind": "choice", "options": YES_NO, "default": "Yes"},

    {"section": "Education"},
    {"key": "school", "label": "School", "kind": "text"},
    {"key": "degree", "label": "Degree", "kind": "text"},
    {"key": "major", "label": "Major", "kind": "text"},
    {"key": "graduation", "label": "Graduation (month year)", "kind": "text"},
    {"key": "gpa", "label": "GPA (optional)", "kind": "text"},

    {"section": "Availability"},
    {"key": "start_date", "label": "Earliest start date", "kind": "text", "help": "e.g. Immediately, or 2026-12-01"},
    {"key": "relocate", "label": "Willing to relocate?", "kind": "choice", "options": YES_NO},
    {"key": "salary", "label": "Salary expectation (optional)", "kind": "text",
     "help": "Leave blank and forms that require it will be sent to you to answer"},
    {"key": "referral_source", "label": "How did you hear about us? (default answer)", "kind": "text",
     "default": "Online job board"},

    {"section": "Voluntary self-identification (optional)"},
    {"key": "gender", "label": "Gender", "kind": "choice",
     "options": [DECLINE, "Male", "Female", "Non-binary"], "default": DECLINE},
    {"key": "race", "label": "Race / ethnicity", "kind": "choice",
     "options": [DECLINE, "Asian", "Black or African American", "Hispanic or Latino",
                 "White", "Two or more races", "Native Hawaiian or Pacific Islander",
                 "American Indian or Alaska Native"], "default": DECLINE},
    {"key": "veteran", "label": "Veteran status", "kind": "choice",
     "options": [DECLINE, "I am not a protected veteran", "I am a protected veteran"], "default": DECLINE},
    {"key": "disability", "label": "Disability status", "kind": "choice",
     "options": [DECLINE, "No, I do not have a disability", "Yes, I have a disability"], "default": DECLINE},
]

KEYS = [f["key"] for f in FIELDS if "key" in f]
_BY_KEY = {f["key"]: f for f in FIELDS if "key" in f}


def defaults() -> dict[str, str]:
    return {k: _BY_KEY[k].get("default", "") for k in KEYS}


def load(private_dir: Path | None = None) -> dict[str, str]:
    private_dir = private_dir or PRIVATE_DIR
    path = private_dir / PROFILE_FILE
    data = defaults()
    if path.exists():
        stored = json.loads(path.read_text())
        data.update({k: str(v) for k, v in stored.items() if k in _BY_KEY})
    return data


def clean(values: dict[str, Any]) -> dict[str, str]:
    """Keep known fields only, as trimmed strings; choices must be valid."""
    out = defaults()
    for key, value in values.items():
        field = _BY_KEY.get(key)
        if field is None:
            continue
        value = str(value if value is not None else "").strip()[:2000]
        if field["kind"] == "choice" and value not in field["options"]:
            raise ValueError(f"{field['label']}: '{value}' is not one of the options")
        out[key] = value
    return out


def save(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, str]:
    private_dir = private_dir or PRIVATE_DIR
    data = clean(values)
    private_dir.mkdir(parents=True, exist_ok=True)
    path = private_dir / PROFILE_FILE
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    return data


def missing_required(data: dict[str, str]) -> list[str]:
    """Fields auto-apply can't work without."""
    required = ["first_name", "last_name", "email", "phone", "authorized_us",
                "sponsorship_now", "sponsorship_future"]
    return [_BY_KEY[k]["label"] for k in required if not data.get(k)]


def has_resume(private_dir: Path | None = None) -> bool:
    private_dir = private_dir or PRIVATE_DIR
    return (private_dir / RESUME_FILE).exists()


def save_resume(pdf: bytes, private_dir: Path | None = None) -> None:
    private_dir = private_dir or PRIVATE_DIR
    if len(pdf) > MAX_RESUME_BYTES:
        raise ValueError("Resume is larger than 10 MB")
    if not pdf.startswith(b"%PDF"):
        raise ValueError("That file isn't a PDF")
    private_dir.mkdir(parents=True, exist_ok=True)
    path = private_dir / RESUME_FILE
    path.write_bytes(pdf)
    path.chmod(0o600)


# ── Saved answers to specific questions ──────────────────────────────────
# Questions the rules can't answer ("How would you describe your experience
# with Twitch?") are listed on the My details page, grouped by company.
#
# An answer applies ONLY to the company that asked, unless you tick "use for
# every company" — so company-specific answers never leak into your general
# profile. Questions are normalized and the asking company's name becomes
# "{company}", so a shared answer to "Are you currently a Twitch employee?"
# also answers "Are you currently a Stripe employee?".
#
# answers.json:          {"all": {question: answer},
#                         "companies": {company_key: {question: answer}}}
# pending_questions.json {company: {question: {"label", "options"}}}

ANSWERS_FILE = "answers.json"
PENDING_FILE = "pending_questions.json"


def normalize_question(label: str, company: str = "") -> str:
    q = label.lower().replace("*", " ")
    q = re.sub(r"\((select one|required|optional|select all that apply)\)", " ", q)
    if company:
        for name in {company.lower(), company.lower().replace(" ", "")}:
            if len(name) >= 2:
                q = re.sub(rf"\b{re.escape(name)}\b", "{company}", q)
    return " ".join(q.split()).strip(" :")


def company_key(company: str) -> str:
    return " ".join(company.lower().split())


def _write_private(name: str, data: Any, private_dir: Path) -> None:
    private_dir.mkdir(parents=True, exist_ok=True)
    path = private_dir / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def load_answers(private_dir: Path | None = None) -> dict[str, dict]:
    path = (private_dir or PRIVATE_DIR) / ANSWERS_FILE
    data = json.loads(path.read_text()) if path.exists() else {}
    if data and "all" not in data and "companies" not in data:
        data = {"all": data, "companies": {}}          # old flat format → shared answers
    return {"all": data.get("all", {}), "companies": data.get("companies", {})}


def lookup_answer(saved: dict | None, question: str, company: str) -> str | None:
    """This company's answer first, then answers shared across companies."""
    if not saved:
        return None
    mine = saved.get("companies", {}).get(company_key(company), {})
    if question in mine:
        return mine[question]
    return saved.get("all", {}).get(question)


def save_answers(items: list[dict], private_dir: Path | None = None) -> dict[str, dict]:
    """items: {"question", "answer", "company", "everywhere": bool}.
    Empty answer = forget it. Answered questions leave the pending list."""
    private_dir = private_dir or PRIVATE_DIR
    saved = load_answers(private_dir)
    pending = load_pending(private_dir)
    for it in items:
        q = normalize_question(str(it.get("question", "")))
        a = str(it.get("answer") or "").strip()[:4000]
        company = str(it.get("company") or "")
        everywhere = bool(it.get("everywhere")) or not company
        if not q:
            continue
        bucket = saved["all"] if everywhere else saved["companies"].setdefault(company_key(company), {})
        if a:
            bucket[q] = a
            if everywhere:          # one shared answer settles it for everyone
                for c in list(pending):
                    pending[c].pop(q, None)
            elif company in pending:
                pending[company].pop(q, None)
        else:
            bucket.pop(q, None)
    saved["companies"] = {c: qs for c, qs in saved["companies"].items() if qs}
    _write_private(ANSWERS_FILE, saved, private_dir)
    save_pending({c: qs for c, qs in pending.items() if qs}, private_dir)
    return saved


def load_pending(private_dir: Path | None = None) -> dict[str, dict]:
    path = (private_dir or PRIVATE_DIR) / PENDING_FILE
    data = json.loads(path.read_text()) if path.exists() else {}
    # Old format {question: {label, options, companies: [...]}} → per company
    if data and all(isinstance(v, dict) and "companies" in v for v in data.values()):
        converted: dict[str, dict] = {}
        for q, v in data.items():
            for c in v.get("companies") or ["(unknown)"]:
                converted.setdefault(c, {})[q] = {"label": v.get("label", q), "options": v.get("options", [])}
        data = converted
    return data


def save_pending(pending: dict[str, dict], private_dir: Path | None = None) -> None:
    _write_private(PENDING_FILE, pending, private_dir or PRIVATE_DIR)


def add_pending(items: list[dict], private_dir: Path | None = None) -> None:
    """Record questions an application couldn't answer, under the company
    that asked. items: {"question": normalized, "label", "options", "company"}"""
    pending = load_pending(private_dir)
    saved = load_answers(private_dir)
    for it in items:
        q, company = it["question"], it.get("company") or "(unknown)"
        if not q or lookup_answer(saved, q, company) is not None:
            continue
        entry = pending.setdefault(company, {}).setdefault(q, {"label": it["label"], "options": []})
        entry["options"] = it.get("options") or entry["options"]
        if it.get("multi"):
            entry["multi"] = True          # "select all that apply": tick several
    save_pending(pending, private_dir)


# ── Resume parsing: pre-fill what can be read reliably ───────────────────

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_LINKEDIN = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+/?", re.I)
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", re.I)
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_CAPWORD = r"[A-Z][\w.&'-]*"
# "New York University" / "Stanford University" / "University of Texas at Austin".
# Stops at the keyword (or a few words after "of"), so a right-column city
# on the same line ("New York, NY") isn't swallowed.
_SCHOOL = re.compile(
    rf"(?:{_CAPWORD} ){{0,4}}(?:University|College|Institute of Technology)\b"
    rf"(?: of (?:{_CAPWORD}(?: (?:at|of) )?){{1,4}})?")
_DEGREE = re.compile(
    rf"((?:Master|Bachelor|Doctor) of (?:Science|Arts|Engineering|Technology|Business Administration)"
    rf"|M\.S\.|B\.S\.|B\.Tech\.?|M\.Eng\.?|MS|BS|MBA|PhD)"
    rf"(?:,? in ((?:(?!{_MONTH}\b){_CAPWORD} ?)+))?")
_GRAD = re.compile(rf"(?:Expected\s+)?{_MONTH}\s+20\d\d")
# pypdf sometimes splits a capital from the rest of its word ("New Y ork")
_SPLIT_CAP = re.compile(r"\b([B-HJ-Z]) (?=[a-z]{2,})")


def _url(s: str) -> str:
    s = s.rstrip("/")
    return s if s.lower().startswith("http") else f"https://{s}"


def resume_text(pdf: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_resume(text: str) -> dict[str, str]:
    """Best-effort extraction. Only fields found are returned — the page
    fills them into empty boxes for you to confirm, never overwrites."""
    found: dict[str, str] = {}
    text = _SPLIT_CAP.sub(r"\1", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if lines:  # name is almost always the first line of a resume
        words = re.sub(r"[^A-Za-z .'-]", "", lines[0]).split()
        if 2 <= len(words) <= 4:
            found["first_name"] = words[0].title()
            found["last_name"] = " ".join(words[1:]).title()
    if m := _EMAIL.search(text):
        found["email"] = m.group(0)
    if m := _PHONE.search(text):
        found["phone"] = m.group(0).strip()
    if m := _LINKEDIN.search(text):
        found["linkedin"] = _url(m.group(0))
    if m := _GITHUB.search(text):
        found["github"] = _url(m.group(0))
    if m := _SCHOOL.search(text):
        found["school"] = m.group(0).strip(" ,")
        # Degree and dates usually sit on the line(s) right after the school
        window = text[m.start(): m.start() + 300]
        if d := _DEGREE.search(window):
            found["degree"] = d.group(1).strip()
            if d.group(2):
                found["major"] = d.group(2).strip()
            # "Aug 2024 – May 2026": graduation is the LAST date on the degree line
            line_end = window.find("\n", d.end())
            dates = _GRAD.findall(window[d.start(): line_end if line_end != -1 else None])
            if not dates:
                dates = _GRAD.findall(window)
            if dates:
                found["graduation"] = dates[-1].replace("Expected", "").strip()
    return found
