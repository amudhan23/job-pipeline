"""Tailor the resume to one job, then build the PDF that gets uploaded.

Claude (Haiku) reads the job description and arranges YOUR resume for it:
which IBM / Sahara AI bullets lead, which 3 projects appear, and which
languages come first. It only chooses and orders what's already in your
resume (config/profile.yaml → resume) — it never writes or changes a
bullet, so nothing on the page is invented. Without AI, the keyword rules
in generator.py do the same arranging.

Output: data/private/applications/<job>/<First>_<Last>_Resume.pdf (the
name a recruiter sees) plus tailor.json saying what was chosen and why.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from pipeline.models import Job, ResumeConfig

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "generic.tex"

_SYSTEM = """You arrange one applicant's resume for one job. You may only \
CHOOSE and ORDER the items given — never rewrite, add or invent anything.

Put first what best matches what the job description asks for (its core \
stack, the kind of systems, the responsibilities). Reply with JSON only:
{"ibm": [all IBM bullet keys, best first],
 "sahara": [all Sahara bullet keys, best first],
 "projects": [exactly 3 project names, best first],
 "languages": [all languages, best first],
 "note": "<one sentence: what the job emphasises and what you led with>"}"""


def resume_bank(config: dict[str, Any]) -> dict[str, Any]:
    r = config.get("resume", {})
    return {
        "ibm": dict(r.get("ibm_bullets", {})),
        "sahara": dict(r.get("sahara_bullets", {})),
        "projects": {p.get("name", k): p.get("desc", "") for k, p in (r.get("projects") or {}).items()
                     if isinstance(p, dict)},
        "languages": list(r.get("language_order_default",
                                ["Java", "Rust", "Python", "TypeScript", "JavaScript", "C++", "C",
                                 "SQL", "Bash"])),
    }


def _order(chosen: list, allowed: list) -> list:
    """Keep the chosen order of valid items, then any left out — so a
    sloppy reply can reorder but never drop or add."""
    picked = [c for c in dict.fromkeys(chosen or []) if c in allowed]
    return picked + [a for a in allowed if a not in picked]


def ai_config(job: dict[str, Any], config: dict[str, Any], private_dir: Path | None = None,
              client: Any = None) -> tuple[ResumeConfig, str] | None:
    """Claude's arrangement, validated. None if AI isn't available or the reply is unusable."""
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    bank = resume_bank(config)
    if not bank["ibm"] or not bank["projects"]:
        return None
    items = json.dumps({"ibm_bullets": bank["ibm"], "sahara_bullets": bank["sahara"],
                        "projects": bank["projects"], "languages": bank["languages"]}, indent=1)
    prompt = f"{job_block(job, limit=10000)}\n\n<resume_items>\n{items}\n</resume_items>"
    try:
        reply = ai.complete(_SYSTEM, prompt, purpose=f"tailor: {job.get('company')} — "
                            f"{job.get('title', '')[:50]}", max_tokens=400,
                            private_dir=private_dir, client=client, model="haiku")
    except Exception:                # no AI / API error → keyword rules instead
        return None
    m = re.search(r"\{.*\}", reply.text, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return None
    projects = _order(data.get("projects"), list(bank["projects"]))[:3]
    return ResumeConfig(
        job_id=job["id"],
        ibm_bullet_order=_order(data.get("ibm"), list(bank["ibm"])),
        sahara_bullet_order=_order(data.get("sahara"), list(bank["sahara"])),
        projects=projects,
        language_order=_order(data.get("languages"), bank["languages"]),
    ), str(data.get("note", ""))[:300]


def resume_filename(details: dict[str, str]) -> str:
    name = f"{details.get('first_name', '')}_{details.get('last_name', '')}".strip("_")
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)
    return f"{name}_Resume.pdf" if name else "Resume.pdf"


def can_build() -> tuple[bool, str]:
    if not TEMPLATE.exists():
        return False, f"no resume template at {TEMPLATE}"
    if not shutil.which("pdflatex"):
        return False, "pdflatex isn't installed (sudo apt install texlive-latex-extra)"
    return True, ""


def tailored_resume(job: dict[str, Any], config: dict[str, Any], details: dict[str, str],
                    folder: Path, use_ai: bool = True, private_dir: Path | None = None,
                    client: Any = None) -> tuple[Path | None, str]:
    """Build (or reuse) this job's resume. Returns (pdf, note); pdf None on failure."""
    from pipeline.resume.generator import ResumeGenerator
    name = resume_filename(details)
    pdf, info = folder / name, folder / "tailor.json"
    if pdf.exists() and info.exists():
        return pdf, json.loads(info.read_text()).get("note", "")
    ok, why = can_build()
    if not ok:
        return None, why
    chosen = ai_config(job, config, private_dir, client) if use_ai else None
    gen = ResumeGenerator(TEMPLATE, config)
    fields = {k: v for k, v in job.items() if k in Job.__dataclass_fields__}
    for k in ("title", "company", "company_slug", "location", "url", "description"):
        fields[k] = fields.get(k) or ""
    fields.setdefault("platform", job.get("platform") or "other")
    job_obj = Job(**fields)
    rc, note = chosen if chosen else (gen.analyze_job(job_obj), "arranged by keyword rules")
    folder.mkdir(parents=True, exist_ok=True)
    built = gen.build(job_obj, folder, config=rc, filename=name)
    if not built:
        return None, "the PDF didn't build (see the log)"
    info.write_text(json.dumps({"note": note, "by": "ai" if chosen else "keywords",
                                "ibm": rc.ibm_bullet_order, "sahara": rc.sahara_bullet_order,
                                "projects": rc.projects, "languages": rc.language_order}, indent=2))
    return built, note
