"""Interview prep: once an application turns into an interview, a pack
written for that loop.

Offer rates have fallen to under 40% of interview processes (Interview Query,
2026), and most lost interviews come down to avoidable preparation, so the
pack does the preparation a coach would:

  1. research (Claude with web search): what the company does and is doing
     now, the tech it runs, and how its interview loop for this kind of role
     is reported to go (rounds, formats, recurring question themes);
  2. the pack (the resume model, from the career profile): per round, the
     questions most likely to come and, for every behavioural one, the best
     story from the profile laid out situation / action / result; gaps the
     interviewer will probe and how to answer them honestly; questions to ask;
     a two-day plan.

Every story line is checked against the career profile the way resume
bullets are, so nothing is rehearsed that didn't happen. Saved next to the
job's resume as prep.json; a thank-you note can be drafted afterwards.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.ai import client as ai
from pipeline.ai.context import job_block
from pipeline.resume import writer

PREP_FILE = "prep.json"

_RESEARCH_SYSTEM = """You prepare a candidate for a job interview. Use web search (at most
6 searches) to learn, from public pages only:
- what the company builds and sells, how big it is, anything notable from the
  last few months (launches, funding, incidents, hiring pushes);
- the technology it is known to run (engineering blog, job posts, GitHub);
- how its interview process for this kind of role is reported to go: rounds,
  formats (coding, system design, take-home, behavioural, pairing), and the
  themes or questions people report (Glassdoor, Blind, dataford, Reddit,
  Levels, company careers pages).
Say what you could not find instead of guessing. Reply with JSON only:
{"company": "<2-4 sentences>", "recent": ["<dated item>"], "stack": ["<tech>"],
 "process": [{"round": "<name>", "format": "<what happens>", "themes": ["<what is asked>"]}],
 "sources": ["<url>"], "unknown": "<what you could not find>"}"""

_PACK_SYSTEM = """You are the candidate's interview coach. You have their career profile
(the only source of facts about them), the job posting, and research on the
company and its interview loop. Write the prep pack for THIS loop.

Rules:
- Stories: only events in the career profile, each told under its own role
  or project, with its real numbers. Never invent a situation, a metric or a
  teammate. If the profile has no story for a question, say so and suggest
  the honest answer ("I haven't done X; the closest is Y").
- Questions: specific to this company's loop and this posting, not a generic
  list. For a coding round name the topics and the kinds of problems this
  company is reported to ask; for system design, the systems close to their
  product; for behavioural, the values the posting states.
- Gaps: what the interviewer will probe (requirements the profile only
  partly meets) and a truthful answer for each.
- Plain language, no em dashes, no filler.
Reply with JSON only:
{"headline": "<one sentence: what this loop is really testing>",
 "rounds": [{"round": "<name>", "format": "<what to expect>",
             "questions": [{"q": "<question>", "kind": "technical|design|behavioural|other",
                            "answer": "<how to approach it, or the story: Situation. Action. Result., 60-120 words>",
                            "facts": ["<each profile fact used, verbatim-ish>"]}]}],
 "stories": [{"name": "<short name>", "from": "<role or project>", "situation": "...", "action": "...", "result": "...",
              "use_for": ["<question themes it answers>"]}],
 "gaps": [{"probe": "<what they will ask>", "answer": "<honest answer>"}],
 "ask_them": ["<question for the interviewer>"],
 "plan": [{"when": "<e.g. Day 1 morning>", "do": "<concrete task>"}]}"""

_THANKS_SYSTEM = """Write a short thank-you email (90-140 words) from the candidate to the
interviewer after an interview. Plain, specific, warm, no flattery, no em
dashes. Mention one concrete thing discussed (from the notes given) and one
reason the candidate is a fit, drawn only from the career profile. Reply
with JSON only: {"subject": "...", "body": "..."}"""


_S = {"type": "string"}
_SL = {"type": "array", "items": _S}
_O = writer._obj
PACK_SCHEMA = _O({
    "headline": _S,
    "rounds": {"type": "array", "items": _O({
        "round": _S, "format": _S,
        "questions": {"type": "array", "items": _O(
            {"q": _S, "kind": {"type": "string", "enum": ["technical", "design", "behavioural", "other"]},
             "answer": _S, "facts": _SL}, ["q", "kind", "answer", "facts"])}}, ["round", "format", "questions"])},
    "stories": {"type": "array", "items": _O(
        {"name": _S, "from": _S, "situation": _S, "action": _S, "result": _S, "use_for": _SL},
        ["name", "from", "situation", "action", "result", "use_for"])},
    "gaps": {"type": "array", "items": _O({"probe": _S, "answer": _S}, ["probe", "answer"])},
    "ask_them": _SL,
    "plan": {"type": "array", "items": _O({"when": _S, "do": _S}, ["when", "do"])},
}, ["headline", "rounds", "stories", "gaps", "ask_them", "plan"])
RESEARCH_SCHEMA = _O({
    "company": _S, "recent": _SL, "stack": _SL,
    "process": {"type": "array", "items": _O({"round": _S, "format": _S, "themes": _SL}, ["round", "format", "themes"])},
    "sources": _SL, "unknown": _S,
}, ["company", "recent", "stack", "process", "sources", "unknown"])


def _path(folder: Path) -> Path:
    return folder / PREP_FILE


def load(folder: Path) -> dict[str, Any] | None:
    p = _path(folder)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def _json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the reply wasn't JSON")
    return json.loads(m.group(0))


def research(job: dict[str, Any], private_dir: Path | None = None, client: Any = None,
             searches: int = 6) -> dict[str, Any]:
    """Company, stack and reported interview loop, from public pages."""
    reply = ai.complete(_RESEARCH_SYSTEM, job_block(job, 6000), purpose=f"interview research: {job.get('company')}",
                        max_tokens=1500, private_dir=private_dir, client=client, model="sonnet",
                        web_search=searches)
    data = writer.parse_json(reply.text, private_dir, client)
    return {"company": str(data.get("company", ""))[:1200], "recent": [str(x)[:200] for x in data.get("recent") or []][:6],
            "stack": [str(x)[:60] for x in data.get("stack") or []][:20],
            "process": [{"round": str(r.get("round", ""))[:80], "format": str(r.get("format", ""))[:300],
                         "themes": [str(t)[:160] for t in r.get("themes") or []][:8]}
                        for r in data.get("process") or [] if isinstance(r, dict)][:8],
            "sources": [str(s)[:300] for s in data.get("sources") or [] if str(s).startswith("http")][:10],
            "unknown": str(data.get("unknown", ""))[:400], "cost": round(reply.cost, 4)}


def _check_stories(pack: dict[str, Any], profile: str) -> list[dict[str, str]]:
    """Every fact a story or answer leans on must be in the profile; the
    ones that aren't are listed (and the answer is marked) rather than
    silently rehearsed."""
    flagged: list[dict[str, str]] = []
    for r in pack.get("rounds") or []:
        for q in r.get("questions") or []:
            bad = [f for f in q.get("facts") or [] if not writer.supported(f, profile)[0]]
            if bad:
                q["unsupported"] = bad
                flagged.append({"where": q.get("q", "")[:80], "facts": "; ".join(bad)[:300]})
    for s in pack.get("stories") or []:
        line = f"{s.get('situation', '')} {s.get('action', '')} {s.get('result', '')}"
        ok, why = writer.supported(line, profile)
        if not ok:
            s["unsupported"] = why
            flagged.append({"where": s.get("name", "")[:80], "facts": why[:300]})
    return flagged


def prep(job: dict[str, Any], folder: Path, private_dir: Path | None = None, client: Any = None,
         notes: str = "") -> dict[str, Any]:
    """Research, then the pack; saved as prep.json in the job's folder."""
    profile = writer.load_profile(private_dir)
    if not profile.strip():
        raise RuntimeError("add your career profile first: the pack is written from it")
    try:
        found = research(job, private_dir, client)
    except Exception as e:                     # the pack still gets written, from the posting alone
        found = {"company": "", "recent": [], "stack": [], "process": [], "sources": [],
                 "unknown": f"research failed: {str(e)[:160]}", "cost": 0.0}
    system = writer._system(profile, _PACK_SYSTEM)
    prompt = job_block(job, 10000) + "\n\n<research>\n" + json.dumps(
        {k: found[k] for k in ("company", "recent", "stack", "process", "unknown")}, indent=1) + "\n</research>"
    if notes.strip():
        prompt += f"\n\n<candidate_notes>\n{notes.strip()[:3000]}\n</candidate_notes>"
    reply = ai.complete(system, prompt, purpose=f"interview pack: {job.get('company')}", max_tokens=7000,
                        private_dir=private_dir, client=client, model=writer._resume_model(private_dir),
                        thinking=3000, schema=PACK_SCHEMA)
    if reply.stop_reason == "max_tokens":
        raise RuntimeError("the pack was cut off (too long); try again")
    pack = writer.parse_json(reply.text, private_dir, client)
    flagged = _check_stories(pack, profile)
    data = {"job_id": job.get("id"), "company": job.get("company"), "title": job.get("title"),
            "research": found, "pack": pack, "flagged": flagged,
            "cost": round(found.get("cost", 0) + reply.cost, 4), "built_at": datetime.now().isoformat(timespec="minutes")}
    folder.mkdir(parents=True, exist_ok=True)
    _path(folder).write_text(json.dumps(data, indent=1))
    return data


def thank_you(job: dict[str, Any], folder: Path, interviewer: str, notes: str, private_dir: Path | None = None,
              client: Any = None) -> dict[str, str]:
    """A thank-you note after a round, from your notes on what was discussed."""
    profile = writer.load_profile(private_dir)
    system = writer._system(profile, _THANKS_SYSTEM)
    prompt = (job_block(job, 3000) + f"\n\nInterviewer: {interviewer or 'the interviewer'}\n"
              f"What was discussed (candidate's notes):\n{notes.strip()[:2000] or '(no notes given)'}")
    reply = ai.complete(system, prompt, purpose=f"thank-you note: {job.get('company')}", max_tokens=600,
                        private_dir=private_dir, client=client, model="sonnet")
    d = _json(reply.text)
    note = {"subject": str(d.get("subject", ""))[:120].replace("—", "-"),
            "body": str(d.get("body", ""))[:1500].replace("—", "-"),
            "at": datetime.now().isoformat(timespec="minutes")}
    data = load(folder) or {}
    data.setdefault("thank_you", []).append({"interviewer": interviewer, **note})
    folder.mkdir(parents=True, exist_ok=True)
    _path(folder).write_text(json.dumps(data, indent=1))
    return note
