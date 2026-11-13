"""AI fit check: before applying, Claude (Haiku — a fraction of a cent per job)
reads the job description against your resume and saved details and looks
for dealbreakers the keyword filters miss:

  - security clearance required / U.S. citizenship or "U.S. person" (export
    control) required / green card only
  - explicitly no visa sponsorship (when you need it)
  - experience or degree requirements well beyond yours (e.g. 5+ years, PhD)
  - seniority far above yours (Staff/Principal), or a non-software role

Verdict: "apply" (no dealbreakers), "maybe" (worth a human look),
"skip" (a real dealbreaker, quoted from the JD). Stored on the job in the
tracker, so each job is checked once.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.ai import client as ai
from pipeline.ai.context import applicant_block, job_block

VERDICTS = ("apply", "maybe", "skip")

_SYSTEM = """You screen job postings for one applicant before they apply.

Find DEALBREAKERS: requirements in the job description the applicant clearly \
cannot meet, judged against their resume and details. Check:
1. Security clearance required (active or ability to obtain one when that \
needs U.S. citizenship).
2. U.S. citizenship, "U.S. person" status (export control / ITAR / EAR), or \
permanent residency required, when the applicant isn't one.
3. Explicitly no visa sponsorship, when the applicant needs sponsorship now \
or in the future.
4. Minimum years of experience well above the applicant's (count their \
full-time experience from the resume; internships count little), or a \
required degree they don't have (e.g. PhD).
5. Seniority far above theirs (Staff, Principal, Director) or a role that \
isn't software engineering at all.

Be strict about evidence: a dealbreaker must be stated in the job \
description — quote the words. "Preferred"/"nice to have"/"bonus" are not \
dealbreakers. Missing information is not a dealbreaker.

Reply with JSON only:
{"verdict": "apply" | "maybe" | "skip",
 "dealbreakers": [{"rule": "<1-5 short name>", "quote": "<exact words from the JD>"}],
 "concerns": ["<short note on soft mismatches, e.g. 'asks 3+ years, you have ~3'>"],
 "summary": "<one sentence: why this verdict>"}
verdict = "skip" if any dealbreaker, "maybe" if only borderline concerns, else "apply"."""


def _parse(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    data = json.loads(m.group(0))
    verdict = str(data.get("verdict", "")).lower()
    if verdict not in VERDICTS:
        raise ValueError(f"bad verdict {verdict!r}")
    breakers = [b for b in data.get("dealbreakers") or [] if isinstance(b, dict) and b.get("quote")]
    if verdict == "skip" and not breakers:
        verdict = "maybe"               # no quoted evidence → don't throw the job away
    return {"verdict": verdict,
            "dealbreakers": [{"rule": str(b.get("rule", ""))[:60], "quote": str(b["quote"])[:240]}
                             for b in breakers],
            "concerns": [str(c)[:200] for c in data.get("concerns") or []][:5],
            "summary": str(data.get("summary", ""))[:300]}


def check_job(job: dict[str, Any], details: dict[str, str], resume_text: str,
              private_dir: Path | None = None, client: Any = None) -> dict[str, Any]:
    """Fit-check one job. Raises ai.AIUnavailable when AI can't be used."""
    if not (job.get("description") or "").strip():
        return {"verdict": "maybe", "dealbreakers": [], "concerns": ["no job description to read"],
                "summary": "Couldn't check: the posting has no description.",
                "checked_at": datetime.now().isoformat(timespec="seconds"), "model": ""}
    system = [{"type": "text", "text": _SYSTEM},
              {"type": "text", "text": applicant_block(details, resume_text),
               "cache_control": {"type": "ephemeral"}}]
    reply = ai.complete(system, job_block(job, limit=12000), purpose=f"fit: {job.get('company')} — "
                        f"{job.get('title', '')[:50]}", max_tokens=500, private_dir=private_dir,
                        client=client, model="haiku")
    try:
        result = _parse(reply.text)
    except (ValueError, json.JSONDecodeError):
        result = {"verdict": "maybe", "dealbreakers": [], "concerns": ["couldn't read the AI reply"],
                  "summary": reply.text[:200]}
    result.update(checked_at=datetime.now().isoformat(timespec="seconds"), model=reply.model)
    return result


def stored(job: dict[str, Any]) -> dict[str, Any] | None:
    raw = job.get("ai_fit") or ""
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def ensure_checked(job: dict[str, Any], tracker, details: dict[str, str], resume_text: str,
                   private_dir: Path | None = None, client: Any = None) -> dict[str, Any] | None:
    """The job's fit result — from the tracker if already checked, else
    checked now and saved. None when AI isn't available."""
    have = stored(job)
    if have:
        return have
    try:
        result = check_job(job, details, resume_text, private_dir, client)
    except ai.AIUnavailable:
        return None
    except Exception as e:           # network / API error: don't block applying
        import logging
        logging.getLogger(__name__).warning("fit check failed: %s", e)
        return None
    tracker.set_ai_fit(job["id"], result)
    job["ai_fit"] = json.dumps(result)
    return result
