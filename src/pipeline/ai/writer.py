"""Draft answers to open questions ("Why Ande?", "What exceptional work have
you done?") from YOUR resume and the job description.

Rules the drafts follow:
  - only facts that are in your resume / saved details — nothing invented;
  - plain, specific, first person, short (what a recruiter actually reads);
  - never used for legal, eligibility, demographic or yes/no questions —
    those come from your saved details or wait for you.

Drafts are cached per company + question (data/private/ai_drafts.json), so
re-running costs nothing, and they show up on My details for you to edit.
Once you save one there it becomes your own answer and is used as-is.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pipeline.ai import client as ai

DRAFTS_FILE = "ai_drafts.json"

# Questions worth a written answer
_OPEN = re.compile(
    r"^why\b|\bwhy (do|are|would|did)\b|what (excites|interests|draws|attracts|motivates)|"
    r"tell us|describe|explain|what (exceptional|impressive|interesting)|"
    r"(share|walk us through|give an example)|what makes you|cover letter|"
    r"anything else|additional information|what would you", re.I)
# ...and never these, however they're worded
_NEVER = re.compile(
    r"sponsor|visa|authori[sz]ed|citizen|clearance|criminal|convict|salary|compensation|"
    r"gender|\brace\b|ethnic|veteran|disabilit|pronoun|\bage\b|date of birth|ssn|social security|"
    r"address|phone|e-?mail|linkedin|github|website|\burl\b|\bname\b|\bgpa\b|start date|"
    r"employment history|previously (worked|employed|applied)|relocat|notice period", re.I)


def is_open_question(label: str, kind: str) -> bool:
    if kind not in ("text", "textarea"):
        return False
    text = " ".join(label.replace("*", " ").split())
    if not text or _NEVER.search(text):
        return False
    return kind == "textarea" or bool(_OPEN.search(text))


_SYSTEM = """You write short answers to job-application questions for one applicant.

Hard rules:
- Use ONLY facts from the applicant's resume and details below. Never invent \
employers, projects, numbers, skills, awards or feelings about the company that \
the inputs don't support. If the inputs can't support a good answer, write the \
best honest answer you can from what is there — don't pad.
- First person, plain and specific. No clichés ("passionate", "thrilled", \
"I am excited to apply", "leverage", "synergy"), no em-dash flourishes, no \
restating the question.
- Connect one or two concrete things from the resume to what the job \
description says the team does.
- 60–120 words unless the question asks for more or less. No greeting or sign-off.
- Output only the answer text."""


def _applicant_block(details: dict[str, str], resume_text: str) -> str:
    keep = ["first_name", "last_name", "city", "school", "degree", "major", "graduation",
            "current_employer", "past_employers", "linkedin", "github", "website"]
    facts = "\n".join(f"{k}: {details[k]}" for k in keep if details.get(k))
    return f"<applicant_details>\n{facts}\n</applicant_details>\n\n<resume>\n{resume_text[:12000]}\n</resume>"


def load_drafts(private_dir: Path | None = None) -> dict[str, dict[str, str]]:
    path = ai._pdir(private_dir) / DRAFTS_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def _save_drafts(drafts: dict, private_dir: Path | None) -> None:
    path = ai._pdir(private_dir) / DRAFTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(drafts, indent=2))
    os.chmod(path, 0o600)


def draft_answer(question: str, job: dict[str, Any], details: dict[str, str], resume_text: str,
                 private_dir: Path | None = None, client: Any = None) -> str:
    """A draft for one question (cached). Raises ai.AIUnavailable."""
    from pipeline.applicant import company_key, normalize_question
    company = job.get("company", "")
    q = normalize_question(question, company)
    drafts = load_drafts(private_dir)
    ck = company_key(company)
    if q in drafts.get(ck, {}):
        return drafts[ck][q]

    jd = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    jd = " ".join(jd.split())[:8000]
    system = [
        {"type": "text", "text": _SYSTEM},
        # Same for every question in a run → cached by the API, billed at ~5%
        {"type": "text", "text": _applicant_block(details, resume_text),
         "cache_control": {"type": "ephemeral"}},
    ]
    prompt = (f"<job>\nCompany: {company}\nTitle: {job.get('title', '')}\n"
              f"Description: {jd or '(not available)'}\n</job>\n\n"
              f"Question on the application form: {question.strip()}")
    reply = ai.complete(system, prompt, purpose=f"answer: {company} — {question[:60]}",
                        max_tokens=400, private_dir=private_dir, client=client)
    text = reply.text.strip().strip('"').strip()
    if text:
        drafts.setdefault(ck, {})[q] = text
        _save_drafts(drafts, private_dir)
    return text


def make_writer(job: dict[str, Any], details: dict[str, str], private_dir: Path | None = None,
                client: Any = None):
    """A function filler.fill_form can call for questions nobody answered.
    Returns None for questions it shouldn't write, or when AI isn't set up."""
    from pipeline import applicant
    ready, _ = ai.status(private_dir)
    if not ready:
        return None
    pdir = ai._pdir(private_dir)
    resume_path = pdir / applicant.RESUME_FILE
    try:
        resume_text = applicant.resume_text(resume_path.read_bytes()) if resume_path.exists() else ""
    except Exception:
        resume_text = ""
    if not resume_text.strip():
        return None              # nothing to ground the answer in — don't write

    def write(label: str, kind: str) -> str | None:
        if not is_open_question(label, kind):
            return None
        try:
            return draft_answer(label, job, details, resume_text, private_dir, client) or None
        except ai.AIUnavailable:
            return None
    return write
