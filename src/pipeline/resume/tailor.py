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
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pipeline.models import Job, ResumeConfig

logger = logging.getLogger(__name__)

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


def resume_filename(details: dict[str, str], company: str = "") -> str:
    """firstLast_Company.pdf (amudhanMuthaiah_Clera.pdf): the company in the
    name says, to you and to them, that it was written for this job."""
    first = re.sub(r"[^A-Za-z0-9]", "", details.get("first_name", "")).lower()
    last = re.sub(r"[^A-Za-z0-9]", "", details.get("last_name", ""))
    name = first + (last[:1].upper() + last[1:] if last else "")
    co = re.sub(r"[^A-Za-z0-9]+", "", re.sub(r"\b(inc|llc|ltd|corp|co)\b\.?", "", company, flags=re.I))[:24]
    base = name or "Resume"
    return f"{base}_{co}.pdf" if co else f"{base}_Resume.pdf"


def can_build() -> tuple[bool, str]:
    if not TEMPLATE.exists():
        return False, f"no resume template at {TEMPLATE}"
    if not shutil.which("pdflatex"):
        return False, "pdflatex isn't installed (sudo apt install texlive-latex-recommended texlive-latex-extra lmodern)"
    if shutil.which("kpsewhich"):
        r = subprocess.run(["kpsewhich", "lmodern.sty", "cmap.sty", "titlesec.sty", "enumitem.sty"],
                           capture_output=True, text=True)
        found = r.stdout.split()
        missing = [n for n in ("lmodern.sty", "cmap.sty", "titlesec.sty", "enumitem.sty")
                   if not any(f.endswith(n) for f in found)]
        if missing:
            return False, (f"LaTeX is missing {', '.join(missing)} "
                           "(sudo apt install texlive-latex-recommended texlive-latex-extra lmodern)")
    return True, ""


# ── Rewording bullets toward the job (with a fact guard) ───────────────

_REWRITE_SYSTEM = """You tune one applicant's resume bullets toward one job description.

Rules — break any and the bullet is thrown away:
- Keep every fact exactly: same numbers, same technologies, same scope, same \
outcome. Never add a skill, tool, language, metric, team size or claim the \
original bullet doesn't state.
- You MAY: lead with the part the job cares about, use a stronger accurate \
verb, and use the job description's wording for something the bullet \
already says (e.g. "REST APIs" → "RESTful APIs").
- At most the original length plus 10 characters. One sentence, no ending period.
- If a bullet already fits or can't be improved honestly, return it unchanged.

Reply with JSON only:
{"ibm": {"<key>": "<bullet>"}, "sahara": {"<key>": "<bullet>"}, "projects": {"<key>": "<description>"}}"""

# Technology words a rewrite may only use if the original bullet already has them
_TECH = set("""java python go golang rust kotlin scala ruby php swift c c++ c# typescript javascript sql nosql
bash shell node node.js nodejs react angular vue next.js spring springboot django flask fastapi rails .net
kubernetes k8s docker terraform ansible helm aws azure gcp lambda s3 ec2 kafka rabbitmq spark hadoop
airflow flink postgres postgresql mysql redis mongodb dynamodb cassandra elasticsearch snowflake bigquery
graphql grpc rest restful jenkins github gitlab ci/cd ci cd linux unix microservices serverless
pytorch tensorflow ml ai llm llms nlp gemini openai elevenlabs ethereum blockchain solidity webassembly
html css tailwind redux jest pytest junit selenium playwright prometheus grafana datadog splunk
oauth saml sso jwt elf""".split())
_NUM = re.compile(r"\d[\d,.]*\s*(?:%|k|m|x|\+)?", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")


def faithful(original: str, rewrite: str) -> tuple[bool, str]:
    """Does the rewrite keep to the original's facts? (ok, reason if not)"""
    if not rewrite or not rewrite.strip():
        return False, "empty"
    if len(rewrite) > len(original) * 1.15 + 10:
        return False, "too long for one page"
    norm = lambda n: re.sub(r"[\s,]", "", n.lower()).rstrip(".")   # noqa: E731
    have = {norm(n) for n in _NUM.findall(original)}
    for n in _NUM.findall(rewrite):
        if norm(n) not in have:
            return False, f"new number {n.strip()!r}"
    orig_words = {w.lower().rstrip(".") for w in _WORD.findall(original)}
    for w in _WORD.findall(rewrite):
        lw = w.lower().rstrip(".")
        techy = lw in _TECH or any(ch.isdigit() or ch in "+#" for ch in w) or \
            (any(ch.isupper() for ch in w[1:]) and len(w) > 1)
        # "RESTful" for "REST", "PostgreSQL" for "Postgres": the same thing, worded like the JD
        same_thing = any(len(o) >= 3 and (o in lw or lw in o) for o in orig_words)
        if techy and lw not in orig_words and not same_thing:
            return False, f"adds {w!r}"
    # Every technology in the original must survive
    new_words = {x.lower().rstrip(".") for x in _WORD.findall(rewrite)}
    for w in orig_words:
        if w in _TECH and not any(w == n or (len(w) >= 3 and w in n) for n in new_words):
            return False, f"drops {w!r}"
    return True, ""


def ai_rewrite(job: dict[str, Any], config: dict[str, Any], private_dir: Path | None = None,
               client: Any = None) -> tuple[dict[str, Any], list[dict]]:
    """Reworded copy of config["resume"] for this job, plus the change log.
    Rewrites that fail the fact guard keep the original wording."""
    import copy
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    r = config.get("resume", {})
    bank = {"ibm": dict(r.get("ibm_bullets", {})), "sahara": dict(r.get("sahara_bullets", {})),
            "projects": {k: p.get("desc", "") for k, p in (r.get("projects") or {}).items() if isinstance(p, dict)}}
    prompt = f"{job_block(job, limit=10000)}\n\n<bullets>\n{json.dumps(bank, indent=1)}\n</bullets>"
    reply = ai.complete(_REWRITE_SYSTEM, prompt, purpose=f"resume: {job.get('company')} — "
                        f"{job.get('title', '')[:50]}", max_tokens=1200, private_dir=private_dir, client=client)
    m = re.search(r"\{.*\}", reply.text, re.S)
    data = json.loads(m.group(0)) if m else {}
    new = copy.deepcopy(config)
    nr = new.setdefault("resume", {})
    changes: list[dict] = []
    for section, target in (("ibm", "ibm_bullets"), ("sahara", "sahara_bullets"), ("projects", "projects")):
        for key, before in bank[section].items():
            after = str((data.get(section) or {}).get(key, "") or "").strip().rstrip(".")
            if not after or after == before:
                continue
            ok, why = faithful(before, after)
            changes.append({"section": section, "key": key, "before": before, "after": after,
                            "kept": ok, "why": why})
            if ok:
                if section == "projects":
                    nr[target][key]["desc"] = after
                else:
                    nr[target][key] = after
    return new, changes


def _pages(pdf: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 1


def tailored_resume(job: dict[str, Any], config: dict[str, Any], details: dict[str, str],
                    folder: Path, use_ai: bool = True, private_dir: Path | None = None,
                    client: Any = None, rewrite: bool = True) -> tuple[Path | None, str]:
    """Craft (or reuse) this job's resume:
      1. choose and order bullets, projects and languages for the JD;
      2. with AI and rewrite=True, reword bullets toward the JD — any rewrite
         that adds a number or technology the original lacks is discarded;
      3. build the PDF; if rewording pushed it past one page, rebuild without it.
    Returns (pdf, note); pdf None on failure."""
    from pipeline.resume.generator import ResumeGenerator
    name = resume_filename(details, job.get("company") or "")
    pdf, info = folder / name, folder / "tailor.json"
    if info.exists():
        # built before (possibly under the older Name_Resume.pdf name)
        old = folder / json.loads(info.read_text()).get("pdf_name", "")
        if pdf.exists():
            return pdf, json.loads(info.read_text()).get("note", "")
        if old.name and old.exists():
            return old, json.loads(info.read_text()).get("note", "")
    ok, why = can_build()
    if not ok:
        return None, why
    from pipeline.ai import client as _ai
    from pipeline.resume import writer as pw
    if use_ai and pw.has_profile(private_dir):
        # With a career profile, the resume is written from it or not at all:
        # the old bullet bank is generic and would go out under your name.
        ready, why = _ai.status(private_dir)
        if not ready:
            return None, f"your career profile needs Claude to write from, and the AI is unavailable: {why}"
        built, meta = None, {}
        for attempt in range(2):                   # one retry: a cut-off or malformed reply is usually a one-off
            try:
                built, meta = pw.write_resume(job, details, folder, name, private_dir, client)
            except Exception as e:
                logger.warning(f"resume writer, try {attempt + 1}: {type(e).__name__}: {e}")
                built, meta = None, {"error": f"{type(e).__name__}: {e}"[:300]}
                if "timeout" in type(e).__name__.lower() or "budget" in str(e).lower():
                    break                          # trying again won't help, and the page is waiting
            if built:
                break
        if not built:
            why = meta.get("error") or "the PDF didn't build"
            if meta.get("dropped") and not meta.get("error"):
                why = f"nothing in the draft passed the check against your profile ({len(meta['dropped'])} lines dropped)"
            return None, f"the resume writer failed: {why}"
        if built:
            note = meta.get("note") or "written from your career profile"
            if meta.get("dropped"):
                n = len(meta["dropped"])
                note += f" Dropped {n} line{'s' if n > 1 else ''} the profile didn't support."
            info.write_text(json.dumps({"note": note, "by": "profile", "pdf_name": name,
                                        "dropped": meta.get("dropped", []), "trimmed": meta.get("trimmed", []),
                                        "resume": meta.get("resume"), "plan": meta.get("plan"),
                                        "steps": meta.get("steps", []),
                                        "parse_problems": meta.get("parse_problems", []),
                                        "had_description": meta.get("had_description", True),
                                        "built_at": __import__("datetime").datetime.now()
                                        .isoformat(timespec="seconds")}, indent=2))
            return built, note
    fields = {k: v for k, v in job.items() if k in Job.__dataclass_fields__}
    for k in ("title", "company", "company_slug", "location", "url", "description"):
        fields[k] = fields.get(k) or ""
    fields.setdefault("platform", job.get("platform") or "other")
    job_obj = Job(**fields)

    chosen = ai_config(job, config, private_dir, client) if use_ai else None
    rc, note = chosen if chosen else (ResumeGenerator(TEMPLATE, config).analyze_job(job_obj),
                                      "arranged by keyword rules")
    changes: list[dict] = []
    worded = config
    from pipeline.ai import client as ai
    if use_ai and rewrite and (job.get("description") or "").strip() and ai.status(private_dir)[0]:
        try:
            worded, changes = ai_rewrite(job, config, private_dir, client)
        except Exception as e:                     # no AI / API error / bad reply: keep the wording
            changes = [{"error": str(e)[:200]}]
    folder.mkdir(parents=True, exist_ok=True)
    built = ResumeGenerator(TEMPLATE, worded).build(job_obj, folder, config=rc, filename=name)
    kept = [c for c in changes if c.get("kept")]
    if built and kept and _pages(built) > 1:
        built = ResumeGenerator(TEMPLATE, config).build(job_obj, folder, config=rc, filename=name)
        for c in kept:
            c["kept"], c["why"] = False, "pushed the resume past one page"
        kept = []
    if not built:
        return None, "the PDF didn't build (see the log)"
    if kept:
        note += f" Reworded {len(kept)} bullet{'s' if len(kept) > 1 else ''} toward the job."
    info.write_text(json.dumps({"note": note, "by": "ai" if chosen else "keywords", "pdf_name": name,
                                "ibm": rc.ibm_bullet_order, "sahara": rc.sahara_bullet_order,
                                "projects": rc.projects, "languages": rc.language_order,
                                "changes": changes, "built_at": __import__("datetime").datetime.now()
                                .isoformat(timespec="seconds")}, indent=2))
    return built, note
