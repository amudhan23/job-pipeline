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
# with Twitch?") are shown on the My details page; you answer once and the
# answer is reused. Questions are normalized and the company's name becomes
# "{company}", so "Are you currently a Twitch employee?" also answers
# "Are you currently a Stripe employee?".

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


def load_answers(private_dir: Path | None = None) -> dict[str, str]:
    path = (private_dir or PRIVATE_DIR) / ANSWERS_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def save_answers(answers: dict[str, str], private_dir: Path | None = None) -> dict[str, str]:
    """Merge answers in (empty value = forget that answer)."""
    private_dir = private_dir or PRIVATE_DIR
    merged = load_answers(private_dir)
    for q, a in answers.items():
        q = normalize_question(str(q))
        a = str(a or "").strip()[:4000]
        if not q:
            continue
        if a:
            merged[q] = a
        else:
            merged.pop(q, None)
    private_dir.mkdir(parents=True, exist_ok=True)
    path = private_dir / ANSWERS_FILE
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    path.chmod(0o600)
    # Answered questions are no longer pending
    pending = load_pending(private_dir)
    if pending:
        save_pending({q: v for q, v in pending.items() if q not in merged}, private_dir)
    return merged


def load_pending(private_dir: Path | None = None) -> dict[str, dict]:
    path = (private_dir or PRIVATE_DIR) / PENDING_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def save_pending(pending: dict[str, dict], private_dir: Path | None = None) -> None:
    private_dir = private_dir or PRIVATE_DIR
    private_dir.mkdir(parents=True, exist_ok=True)
    path = private_dir / PENDING_FILE
    path.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def add_pending(items: list[dict], private_dir: Path | None = None) -> None:
    """Record questions an application couldn't answer.
    items: {"question": normalized, "label": original, "options": [...], "company": ...}"""
    pending = load_pending(private_dir)
    answered = load_answers(private_dir)
    for it in items:
        q = it["question"]
        if not q or q in answered:
            continue
        entry = pending.setdefault(q, {"label": it["label"], "options": [], "companies": []})
        entry["options"] = it.get("options") or entry["options"]
        if it.get("company") and it["company"] not in entry["companies"]:
            entry["companies"].append(it["company"])
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
