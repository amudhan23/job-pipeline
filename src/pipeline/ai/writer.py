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
from dataclasses import dataclass
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
    from pipeline.ai.context import applicant_block
    return applicant_block(details, resume_text)


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

    from pipeline.ai.context import job_block
    system = [
        {"type": "text", "text": _SYSTEM},
        # Same for every question in a run → cached by the API, billed at ~5%
        {"type": "text", "text": _applicant_block(details, resume_text),
         "cache_control": {"type": "ephemeral"}},
    ]
    prompt = f"{job_block(job)}\n\nQuestion on the application form: {question.strip()}"
    reply = ai.complete(system, prompt, purpose=f"answer: {company} — {question[:60]}",
                        max_tokens=400, private_dir=private_dir, client=client)
    text = reply.text.strip().strip('"').strip()
    if text:
        drafts.setdefault(ck, {})[q] = text
        _save_drafts(drafts, private_dir)
    return text


# ── Picking an answer to a closed question from your resume ────────────

PICKS_FILE = "ai_picks.json"

_PICK_SYSTEM = """You answer one application-form question for one applicant, but ONLY \
when their resume or saved details settle it.

Rules:
- If the question is about something the inputs don't cover (their history \
with this company, which apps or platforms they use, personal preferences, \
who referred them, anything not in the resume/details), answer UNKNOWN.
- If it asks whether they have a specific skill, tool, language or kind of \
experience and the resume doesn't mention it at all, answer the option that \
means no / none, with basis "not in resume".
- Years of experience: count from the resume's dates; full-time roles only \
unless the question says otherwise.
- The answer must be copied exactly from the listed options; for "select all \
that apply" give a comma-separated list of options; for a free-text box give \
a short value (a number or a few words).
- Never guess. UNKNOWN is the right answer whenever you'd be guessing.

Reply with JSON only:
{"answer": "<option text | short value | UNKNOWN>", "basis": "<the resume line or \
detail it rests on, or 'not in resume'>"}"""

_CHOICE = ("select", "combobox", "radio", "buttons", "checkboxes")


@dataclass
class AIAnswer:
    value: str
    how: str            # "draft" (written answer) or "pick" (chosen from your resume)
    basis: str = ""

    @property
    def source(self) -> str:
        if self.how == "draft":
            return "AI draft from your resume — review it"
        if self.basis.lower().startswith("not in"):
            return "AI pick: not in your resume — check it"
        return f"AI pick ({self.basis[:70]}) — check it"


def can_pick(label: str, kind: str) -> bool:
    text = " ".join(label.replace("*", " ").split())
    if not text or _NEVER.search(text) or _NOT_PICKABLE.search(text):
        return False
    return kind in _CHOICE or kind == "text"


# Beyond _NEVER: things only you know, however they're asked
_NOT_PICKABLE = re.compile(r"refer|hear about|learn about|know anyone|related to|family|"
                           r"applied (to|before|previously)|interviewed|how did you find|"
                           r"consent|agree|acknowledge|certify|signature|today.s date", re.I)


def pick_answer(question: str, kind: str, options: list[str], job: dict[str, Any],
                details: dict[str, str], resume_text: str, private_dir: Path | None = None,
                client: Any = None) -> AIAnswer | None:
    """Claude's answer to a closed question, or None if the resume doesn't
    settle it (cached either way). Raises ai.AIUnavailable."""
    from pipeline.ai.context import job_block
    from pipeline.applicant import company_key, normalize_question
    from pipeline.apply.answers import pick_option
    company = job.get("company", "")
    q, ck = normalize_question(question, company), company_key(company)
    path = ai._pdir(private_dir) / PICKS_FILE
    cache = json.loads(path.read_text()) if path.exists() else {}
    hit = cache.get(ck, {}).get(q)
    if hit is None:
        opts = [o for o in options if o and o.strip()]
        listing = "\n".join(f"- {o}" for o in opts) if opts else "(free-text box)"
        multi = " (select all that apply)" if kind == "checkboxes" else ""
        prompt = (f"{job_block(job, limit=3000)}\n\nQuestion{multi}: {question.strip()}\n"
                  f"Options:\n{listing}")
        system = [{"type": "text", "text": _PICK_SYSTEM},
                  {"type": "text", "text": _applicant_block(details, resume_text),
                   "cache_control": {"type": "ephemeral"}}]
        reply = ai.complete(system, prompt, purpose=f"pick: {company} — {question[:60]}",
                            max_tokens=200, private_dir=private_dir, client=client)
        m = re.search(r"\{.*\}", reply.text, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
        hit = {"answer": str(data.get("answer", "UNKNOWN")).strip(),
               "basis": str(data.get("basis", "")).strip()}
        cache.setdefault(ck, {})[q] = hit
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2))
        os.chmod(path, 0o600)

    answer, basis = hit["answer"], hit["basis"]
    if not answer or answer.upper() == "UNKNOWN" or not basis:
        return None
    if kind in _CHOICE and options:
        parts = [p.strip() for p in answer.split(",")] if kind == "checkboxes" else [answer]
        matched = [pick_option(p, options) for p in parts]
        if None in matched:
            return None                       # not one of the options → don't force it
        answer = ", ".join(matched)
    return AIAnswer(answer, "pick", basis)


def make_writer(job: dict[str, Any], details: dict[str, str], private_dir: Path | None = None,
                client: Any = None):
    """A function filler.fill_form calls for required questions nobody
    answered: write(label, kind, options) → AIAnswer or None. None when AI
    isn't set up, or there's no resume to ground answers in."""
    from pipeline.ai.context import load_resume_text
    ready, _ = ai.status(private_dir)
    if not ready:
        return None
    resume_text = load_resume_text(private_dir)
    if not resume_text.strip():
        return None              # nothing to ground the answer in — don't write

    def write(label: str, kind: str, options: list[str] | None = None) -> AIAnswer | None:
        try:
            if is_open_question(label, kind):
                text = draft_answer(label, job, details, resume_text, private_dir, client)
                return AIAnswer(text, "draft") if text else None
            if can_pick(label, kind):
                return pick_answer(label, kind, options or [], job, details, resume_text,
                                   private_dir, client)
        except ai.AIUnavailable:
            return None
        except Exception as e:       # network / API error: leave the question for you
            import logging
            logging.getLogger(__name__).warning("AI answer failed: %s", e)
            return None
        return None
    return write
