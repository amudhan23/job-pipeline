"""Write a whole resume for one job from your career profile.

The career profile (data/private/career_profile.md, edited on the
dashboard's Career profile page) holds everything about you: every role,
project, number, skill level and rule. For each job Claude picks what's
relevant and writes the resume — bullets, projects, skills — the way you'd
get it in a chat. Then every line is checked against the profile before
it's printed:

  - an experience bullet may only use numbers and technologies that appear
    in THAT role's section of the profile (so Go can't move into IBM);
  - a project bullet, only what's in that project's entry;
  - skills, only technologies the profile mentions anywhere;
  - companies, titles and projects must exist in the profile.

Lines that fail are dropped (and listed in tailor.json). The page is built
with pdflatex and trimmed bullet by bullet until it fits on one page.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pipeline.resume.tailor import _NUM, _TECH, _WORD

PROFILE_FILE = "career_profile.md"
_CASE_SENSITIVE = {"go", "c", "rest", "r"}     # only tech when written "Go", "C", "REST", "R"
# Plain words of the trade, not claims of a skill: "HTTP proxy", "REST APIs", "on-call"
_GENERIC = {"http", "https", "api", "apis", "json", "url", "urls", "cli", "ui", "ux", "io", "cpu", "cpus", "gpu",
            "gpus", "os", "ci", "cd", "id", "ids", "ai", "ml", "sdk", "sdks", "ssd", "ram", "csv", "pdf", "tcp",
            "ip", "dns", "e2e", "p95", "p99", "qps", "rps", "kb", "mb", "gb", "tb", "ms"}
# Short names for institutions, both ways
_ALIASES = {"nyu": "new york university", "iit": "indian institute of technology",
            "mit": "massachusetts institute of technology", "cmu": "carnegie mellon university"}
_STOP = {"of", "the", "and", "at", "in", "inc", "llc", "ltd", "corp", "co"}


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def load_profile(private_dir: Path | None = None) -> str:
    p = _pdir(private_dir) / PROFILE_FILE
    try:
        return p.read_text()
    except OSError:
        return ""


def save_profile(text: str, private_dir: Path | None = None) -> None:
    import os
    p = _pdir(private_dir) / PROFILE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text[:200_000])
    os.chmod(p, 0o600)


REFERENCE_FILE = "reference_resume.pdf"


def save_reference(pdf: bytes, private_dir: Path | None = None) -> int:
    """A resume whose look and writing you like. Used for style only."""
    from pipeline import applicant
    if not pdf.startswith(b"%PDF"):
        raise ValueError("that isn't a PDF")
    text = applicant.resume_text(pdf)
    path = _pdir(private_dir) / REFERENCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)
    path.chmod(0o600)
    return len(text)


def reference_text(private_dir: Path | None = None) -> str:
    """Your reference resume's text, or your uploaded resume's if you haven't set one."""
    from pipeline import applicant
    from pipeline.ai.context import load_resume_text
    path = _pdir(private_dir) / REFERENCE_FILE
    try:
        if path.exists():
            return applicant.resume_text(path.read_bytes())
    except Exception:
        pass
    return load_resume_text(private_dir)


def has_profile(private_dir: Path | None = None) -> bool:
    return len(load_profile(private_dir).strip()) > 400


# ── Reading the profile into checkable pieces ──────────────────────────

_NOTE = re.compile(r"\[(?:confirm|add|todo)[^\]]*\]", re.I)


def evidence(profile: str) -> str:
    """The profile without your [confirm …]/[add …] notes — a note that
    questions a claim must never count as proof of it."""
    return _NOTE.sub("", profile)


def never_claim(profile: str) -> list[str]:
    """Terms from the Rules line "Never claim: a, b, c" (and "Never list X")."""
    terms: list[str] = []
    for line in rules_section(profile).splitlines():
        m = re.search(r"never claim:?\s*(.*)", line, re.I)
        if not m:
            continue
        for item in _NOTE.sub("", m.group(1)).split(","):
            item = re.split(r"\s+(?:in|beyond|work|or|experience|you|at|unless)\b", item.strip(" .*\"'"))[0]
            terms += [t.strip(" *\"'").rstrip(".") for t in item.split("/") if len(t.strip(" .*\"'")) >= 2]
    return terms


def _mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I) is not None


def sections(profile: str) -> dict[str, str]:
    """Heading (### or ##) → its text, plus each "- **Project**" list entry."""
    profile = evidence(profile)
    out: dict[str, str] = {}
    cur, buf = "_top", []
    for line in profile.splitlines():
        m = re.match(r"^#{2,4}\s+(.*)$", line)
        if m:
            out[cur] = "\n".join(buf)
            cur, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf)
    # Projects written as list entries: "- **AccessMint** (...)"
    entries: dict[str, str] = {}
    for body in list(out.values()):
        for m in re.finditer(r"^- \*\*(.+?)\*\*(.*(?:\n(?:  |\t).*)*)", body, re.M):
            entries[m.group(1).strip()] = m.group(0)
    out.update({f"project:{k}": v for k, v in entries.items()})
    return out


def rules_section(profile: str) -> str:
    for head, body in sections(profile).items():
        if head.lower().startswith("rules"):
            return body
    return ""


def _tokens(text: str) -> set[str]:
    t = re.sub(r"[^a-z0-9]+", " ", text.lower())
    for short, full in _ALIASES.items():
        if re.search(rf"\b{short}\b", t):
            t += " " + full
        if full in t:
            t += " " + short
    return {w for w in t.split() if w not in _STOP}


def _find(secs: dict[str, str], name: str, kind: str) -> str | None:
    """The profile text for an employer or project named in the resume.
    "New York University, Courant Institute" finds "NYU Courant Institute (New York University)"."""
    want = _tokens(name)
    if not want:
        return None
    best, best_score = None, 0.0
    for head, body in secs.items():
        if kind == "project" and not head.startswith("project:"):
            continue
        if kind == "role" and head.startswith("project:"):
            continue
        have = _tokens(head.replace("project:", ""))
        if not have:
            continue
        first = re.sub(r"[^a-z0-9]+", " ", head.lower().replace("project:", "")).split()
        shared = want & have
        score = len(shared) / len(want)
        if first and first[0] in want:          # "IBM" heading, "IBM India" in the resume
            score = max(score, 0.75)
        if score > best_score:
            best, best_score = head + "\n" + body, score
    return best if best_score >= 0.6 else None


def _norm_num(s: str) -> str:
    return re.sub(r"[\s,]", "", s.lower()).rstrip(".")


def supported(line: str, source: str) -> tuple[bool, str]:
    """Every number and technology in `line` appears in `source`."""
    src_nums = {_norm_num(n) for n in _NUM.findall(source)}
    src_flat = _norm_num(source)
    for n in _NUM.findall(line):
        k = _norm_num(n)
        if k not in src_nums and k not in src_flat:
            return False, f"the number {n.strip()!r} isn't in your profile there"
    src_words = {w.lower().rstrip(".") for w in _WORD.findall(source)}
    for w in _WORD.findall(line.replace("**", "")):
        lw = w.lower().rstrip(".")
        if lw in _CASE_SENSITIVE:
            techy = w.rstrip(".") in ("Go", "C", "REST", "R")
        else:
            techy = lw in _TECH or any(ch.isdigit() or ch in "+#" for ch in w) or \
                (any(ch.isupper() for ch in w[1:]) and len(w) > 1)
        if not techy or lw in src_words or lw in _GENERIC:
            continue
        if len(lw) >= 3 and any(len(s) >= 3 and (s.startswith(lw) or lw.startswith(s)) for s in src_words):
            continue
        if lw == "sql" and any(s.endswith("sql") for s in src_words):     # PostgreSQL / MySQL imply SQL
            continue
        return False, f"{w!r} isn't in your profile there"
    return True, ""


def check(resume: dict[str, Any], profile: str) -> tuple[dict[str, Any], list[dict]]:
    """Drop every line the profile doesn't support. Returns (clean, dropped)."""
    secs = sections(profile)
    banned = never_claim(profile)
    proof = evidence(profile)
    skill_proof = "\n".join(l for l in proof.splitlines()
                            if not re.search(r"familiar|coursework|never claim", l, re.I))

    def bad_term(line: str, allowed_in: str = "") -> str:
        for t in banned:
            if _mentions(line, t) and not (allowed_in and _mentions(allowed_in, t)):
                return t
        return ""

    dropped: list[dict] = []
    clean: dict[str, Any] = {"experience": [], "projects": [], "skills": [], "education": [],
                             "extras": [], "note": str(resume.get("note", ""))[:300]}
    for e in resume.get("experience") or []:
        # Company and title together tell apart two roles at one employer
        src = _find(secs, f"{e.get('company', '')} {e.get('title', '')}", "role") or \
            _find(secs, e.get("company", ""), "role")
        if not src:
            dropped.append({"where": e.get("company", "?"), "line": "(whole role)",
                            "why": "that employer isn't in your profile"})
            continue
        if e.get("title") and not supported(e["title"], src)[0]:
            dropped.append({"where": e["company"], "line": e["title"], "why": "title not as in your profile"})
            e["title"] = ""
        for k in ("dates", "location"):
            if e.get(k) and not supported(e[k], src)[0]:
                dropped.append({"where": e["company"], "line": e[k], "why": f"{k} not as in your profile"})
                e[k] = ""
        bullets = []
        for b in e.get("bullets") or []:
            ok, why = supported(b, src)
            t = bad_term(b)
            if t:
                ok, why = False, f"your Rules say never claim {t}"
            (bullets.append(b) if ok else dropped.append({"where": e["company"], "line": b, "why": why}))
        if bullets:
            clean["experience"].append({**e, "bullets": bullets})
    for p in resume.get("projects") or []:
        src = _find(secs, p.get("name", ""), "project") or _find(secs, p.get("name", ""), "role")
        if not src:
            dropped.append({"where": p.get("name", "?"), "line": "(whole project)",
                            "why": "that project isn't in your profile"})
            continue
        stack_ok = supported(p.get("stack", ""), src)[0] or supported(p.get("stack", ""), profile)[0]
        if not stack_ok:
            p["stack"] = ""
        bullets = []
        for b in p.get("bullets") or []:
            ok, why = supported(b, src)
            t = bad_term(b, src)
            if t:
                ok, why = False, f"your Rules say never claim {t}"
            (bullets.append(b) if ok else dropped.append({"where": p["name"], "line": b, "why": why}))
        if bullets:
            clean["projects"].append({**p, "bullets": bullets})
    for s in resume.get("skills") or []:
        items = [i.strip() for i in str(s.get("items", "")).split(",") if i.strip()]
        keep = []
        for i in items:
            ok, why = supported(i, skill_proof)
            t = bad_term(i)
            if t and not _mentions(skill_proof, t):
                ok, why = False, f"your Rules say never claim {t}"
            (keep.append(i) if ok else dropped.append({"where": f"Skills: {s.get('label', '')}", "line": i, "why": why}))
        if keep:
            clean["skills"].append({"label": s.get("label", ""), "items": ", ".join(keep)})
    for ed in resume.get("education") or []:
        if _find(secs, ed.get("school", ""), "role") or ed.get("school", "").split()[0:1] and \
                ed["school"].split()[0].lower() in profile.lower():
            clean["education"].append(ed)
    for x in resume.get("extras") or []:
        ok, why = supported(x.get("text", ""), proof)
        if ok and bad_term(x.get("text", "")):
            ok, why = False, f"your Rules say never claim {bad_term(x.get('text', ''))}"
        (clean["extras"].append(x) if ok else dropped.append({"where": x.get("label", ""), "line": x.get("text", ""),
                                                             "why": why}))
    return clean, dropped


# ── Asking Claude ──────────────────────────────────────────────────────

_SYSTEM = """You write one applicant's resume for one job. The bar is the resume a top
career coach would hand back after an hour with the applicant: dense,
specific, technical, every line earning its place. Recruiters skim for 7
seconds, so the first words of each bullet and the bolded terms must carry it.

FACTS. Use ONLY the applicant's career profile below. Every employer, title,
date, project, number, tool and claim must come from it, and a fact about one
role or project may only appear under that role or project. Follow the
profile's Rules section exactly (it overrides anything here). Never invent a
metric, tool, scale or outcome. Text inside [confirm: ...] or [add: ...] is a
note to the applicant, not a fact. Write employer and project names as the
profile's headings do.

A PLAN made from this job comes with the request. Follow it: its order,
bullet counts, angles, facts and the technologies to show. The rules below
fill in anything it leaves open.

WHAT TO PICK.
- Work experience newest first: always the main full-time job (4-5
  bullets) and internships (2-3 bullets); research roles when they fit the
  job (2 bullets). Teaching, grading and advisory roles only if the page has
  room after the projects. Never drop the main full-time job.
- 3-5 projects, the ones closest to this job's stack and problems first;
  1-2 bullets each.
- About 15-17 bullets in total, which fills one page at 10pt. Don't write
  more expecting them to be cut: overflow is trimmed from the end, not by
  quality.
- Skills: 3-4 lines led by the job's stack, only skills the profile has, at
  the level it states (coursework stays labelled as coursework).
- Education with relevant coursework; publications or awards if the profile
  has them (as extras).

HOW TO WRITE A BULLET.
- Strong past-tense verb, then exactly what was built, how (the specific
  techniques and technologies), and the result with the profile's number.
- Prefer the profile's concrete details (sizes, counts, speedups, design
  choices, bugs found) over generic phrases. Never write filler like "in
  fast-paced sprint cycles", "ensuring data integrity at scale",
  "leveraging cutting-edge", "various", "robust", "seamless".
- Open each bullet with the part of the work that answers one of this
  job's requirements, in the job's own words where honest. The same IBM work
  reads differently for an agents team (production reliability, owning
  services end to end) than for a data team (query tuning, batch jobs).
- A project's "stack" shows only the 2-4 technologies this job cares about,
  never every tool used. Skills list only what's relevant to this job.
- One to two lines each: 120-200 characters, never more. Cut the why-story;
  keep what, how and the result. Bold the 2-3 terms a
  recruiter scans for (**Java Spring Boot**, **10K+ daily requests**, **40%**).
- No em dashes. No summary section unless the Rules allow one.

Reply with JSON only:
{"experience": [{"company": "...", "title": "...", "location": "...", "dates": "...", "bullets": ["..."]}],
 "projects": [{"name": "...", "stack": "...", "link": "...", "context": "e.g. NYU, 4-person team", "bullets": ["..."]}],
 "skills": [{"label": "Languages", "items": "Java, Python, ..."}],
 "education": [{"school": "...", "degree": "...", "dates": "...", "location": "...", "detail": "Coursework: ..."}],
 "extras": [{"label": "Publications", "text": "..."}],
 "note": "<one sentence: what the job emphasises and what you led with>"}"""


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_STR = {"type": "string"}
_BULLETS = {"type": "array", "items": _STR}
RESUME_SCHEMA = _obj({
    "experience": {"type": "array", "items": _obj(
        {"company": _STR, "title": _STR, "location": _STR, "dates": _STR, "bullets": _BULLETS},
        ["company", "title", "location", "dates", "bullets"])},
    "projects": {"type": "array", "items": _obj(
        {"name": _STR, "stack": _STR, "link": _STR, "context": _STR, "bullets": _BULLETS},
        ["name", "stack", "link", "context", "bullets"])},
    "skills": {"type": "array", "items": _obj({"label": _STR, "items": _STR}, ["label", "items"])},
    "education": {"type": "array", "items": _obj(
        {"school": _STR, "degree": _STR, "dates": _STR, "location": _STR, "detail": _STR},
        ["school", "degree", "dates", "location", "detail"])},
    "extras": {"type": "array", "items": _obj({"label": _STR, "text": _STR}, ["label", "text"])},
    "note": _STR,
}, ["experience", "projects", "skills", "education", "extras", "note"])


_PLAN_SYSTEM = """You are a senior technical recruiter and the hiring manager for this job,
planning one applicant's resume before anyone writes it. Think the way a
great coach does in a long conversation with the applicant.

1. The job: what will this person actually do in their first six months?
   Which 4-6 requirements decide the hire (must-haves), which are
   nice-to-haves, what exact terms does the posting use, and what will the
   hiring manager worry about (level, startup pace, production ownership)?
2. Evidence: for each requirement, the applicant's strongest honest evidence
   in the career profile: which role or project and which specific fact.
   Mark it strong, partial or none. Never stretch.
3. The resume: which roles and projects, in what order, how many bullets
   each, the angle for each (which part of that work to lead with for THIS
   job) and the profile facts each should use. Pick projects by relevance
   to this job, not general impressiveness; a project in an unrelated stack
   goes unless it proves a requirement. The main full-time job always stays.
4. For each project, the 2-4 technologies to show: only ones this job cares
   about.
5. Skills: 3-4 lines grouped and named for this job, only relevant skills
   the profile has. Leave out unrelated ones (blockchain tooling for a
   non-blockchain role, say) unless the job asks for them.
6. Extras (publications, awards): only if they help for this job.
About 15-17 bullets in total fills one page. Use only the career profile;
follow its Rules section."""

_REVIEW_SYSTEM = """You are the hiring manager for this job. You have 30 seconds with this
resume draft, then you fix it before it goes out.

Check, and fix what fails:
- The top third shows the must-haves from the plan.
- Every bullet opens with what matters for THIS job. A bullet that is
  generic, vague or about something this job doesn't care about gets
  rewritten from a better fact in the career profile, or replaced.
- Project stacks show 2-4 technologies this job cares about, not every tool.
- Skills hold only what's relevant here; drop the rest.
- Terms from the plan's keyword list that the applicant honestly has are
  present.
- Bullets are 120-200 characters, specific, with the profile's numbers, the
  2-3 scan terms bolded, no filler, no em dashes. About 15-17 in total.

Facts: only from the career profile, each under its own role or project;
follow its Rules. Return the full improved resume. In "note", say in one or
two sentences what the job needs and what you changed."""


def _str_list() -> dict:
    return {"type": "array", "items": {"type": "string"}}


PLAN_SCHEMA = _obj({
    "job_summary": _STR,
    "requirements": {"type": "array", "items": _obj(
        {"need": _STR, "must": {"type": "boolean"}, "evidence": _STR,
         "strength": {"type": "string", "enum": ["strong", "partial", "none"]}},
        ["need", "must", "evidence", "strength"])},
    "keywords": _str_list(),
    "experience": {"type": "array", "items": _obj(
        {"company": _STR, "title": _STR, "bullets": {"type": "integer"}, "angle": _STR, "facts": _str_list()},
        ["company", "title", "bullets", "angle", "facts"])},
    "projects": {"type": "array", "items": _obj(
        {"name": _STR, "bullets": {"type": "integer"}, "angle": _STR, "show_stack": _str_list(),
         "facts": _str_list()},
        ["name", "bullets", "angle", "show_stack", "facts"])},
    "skills": {"type": "array", "items": _obj({"label": _STR, "items": _STR}, ["label", "items"])},
    "extras": _str_list(),
    "leave_out": _str_list(),
}, ["job_summary", "requirements", "keywords", "experience", "projects", "skills", "extras", "leave_out"])


def _resume_model(private_dir: Path | None) -> str:
    from pipeline.ai import client as ai
    # Resumes are worth the better model: never Haiku, even if it's your everyday setting
    model = ai.load_settings(private_dir).get("resume_model", "opus")
    return "sonnet" if model == "haiku" else model


def _system(profile: str, instructions: str) -> list[dict]:
    # The profile goes first and is cached, so the plan, write and review
    # calls all reuse it (cache is by prefix)
    return [{"type": "text", "text": f"<career_profile>\n{profile[:60000]}\n</career_profile>",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": instructions}]


def _job_and_style(job: dict[str, Any], private_dir: Path | None) -> str:
    from pipeline.ai.context import job_block
    prompt = job_block(job, limit=12000)
    current = reference_text(private_dir).strip()
    if current:
        prompt += ("\n\n<reference_resume>\nA resume the applicant likes. Match its STYLE only: how many "
                   "bullets per role, bullet length, tone, how it leads with the job's keywords and what it bolds. "
                   "It is NOT a source of facts: it may overstate things (a title, a location, a skill). Every fact "
                   "must still come from the career profile.\n"
                   f"{current[:8000]}\n</reference_resume>")
    return prompt


def plan(job: dict[str, Any], profile: str, private_dir: Path | None = None, client: Any = None) -> dict[str, Any]:
    """Read the job like a recruiter and decide what the resume should say."""
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    reply = ai.complete(_system(profile, _PLAN_SYSTEM), job_block(job, limit=12000),
                        purpose=f"resume plan: {job.get('company')} — {job.get('title', '')[:50]}",
                        max_tokens=9000, private_dir=private_dir, client=client, thinking=4000,
                        schema=PLAN_SCHEMA, model=_resume_model(private_dir))
    if reply.stop_reason == "max_tokens":
        raise ValueError("the plan was cut off")
    out = parse_json(reply.text, private_dir, client)
    if not out.get("requirements") and not out.get("experience"):
        raise ValueError("the plan came back empty")
    return out


def _check_draft(reply: Any, private_dir: Path | None, client: Any) -> dict[str, Any]:
    if reply.stop_reason == "max_tokens":
        raise ValueError("the reply was cut off before the resume was finished")
    out = parse_json(reply.text, private_dir, client)
    missing = [k for k in ("experience", "projects", "skills", "education") if not out.get(k)]
    if missing:
        raise ValueError(f"the draft came back without {', '.join(missing)}")
    return out


def draft(job: dict[str, Any], profile: str, private_dir: Path | None = None, client: Any = None,
          the_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the resume JSON (from the plan, when there is one)."""
    from pipeline.ai import client as ai
    prompt = _job_and_style(job, private_dir)
    if the_plan:
        prompt += f"\n\n<plan>\n{json.dumps(the_plan, indent=1)[:12000]}\n</plan>"
    reply = ai.complete(_system(profile, _SYSTEM), prompt,
                        purpose=f"resume: {job.get('company')} — {job.get('title', '')[:50]}",
                        max_tokens=12000, private_dir=private_dir, client=client,
                        thinking=0 if the_plan else 4000, schema=RESUME_SCHEMA, model=_resume_model(private_dir))
    return _check_draft(reply, private_dir, client)


def review(job: dict[str, Any], profile: str, the_plan: dict[str, Any] | None, resume: dict[str, Any],
           private_dir: Path | None = None, client: Any = None) -> dict[str, Any]:
    """Read the draft as the hiring manager and return the fixed version."""
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    prompt = job_block(job, limit=12000)
    if the_plan:
        prompt += f"\n\n<plan>\n{json.dumps(the_plan, indent=1)[:12000]}\n</plan>"
    prompt += f"\n\n<draft>\n{json.dumps(resume, indent=1)}\n</draft>"
    reply = ai.complete(_system(profile, _REVIEW_SYSTEM), prompt,
                        purpose=f"resume review: {job.get('company')} — {job.get('title', '')[:50]}",
                        max_tokens=12000, private_dir=private_dir, client=client, thinking=3000,
                        schema=RESUME_SCHEMA, model=_resume_model(private_dir))
    return _check_draft(reply, private_dir, client)


def parse_json(text: str, private_dir: Path | None = None, client: Any = None) -> dict:
    """The resume JSON from a reply. Structured outputs make it valid; if a
    reply still isn't (a model without them), a quick Haiku call repairs it."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the reply had no resume in it" + (" (it was cut off)" if text else ""))
    try:
        return json.loads(m.group(0))
    except ValueError as first:
        from pipeline.ai import client as ai
        fixed = ai.complete("Repair this JSON so it parses: escape stray quotes, add missing commas, close "
                            "brackets. Change nothing else. Reply with the JSON only.", m.group(0),
                            purpose="resume: repair JSON", max_tokens=8000, private_dir=private_dir,
                            client=client, model="haiku")
        try:
            return json.loads(re.search(r"\{.*\}", fixed.text, re.S).group(0))
        except (ValueError, AttributeError):
            raise ValueError(f"the reply wasn't valid JSON ({first})") from first


# ── LaTeX ──────────────────────────────────────────────────────────────

def tex_escape(s: str) -> str:
    s = s.replace(" — ", ", ").replace("—", ", ").replace("–", "--")
    out = []
    for part in re.split(r"(\*\*.+?\*\*)", s):
        bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        text = part[2:-2] if bold else part
        for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
                     ("_", r"\_"), ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"), ("^", r"\^{}")):
            text = text.replace(a, b)
        text = text.replace(r"\textbackslash\{\}", r"\textbackslash{}")
        out.append(rf"\textbf{{{text}}}" if bold else text)
    return "".join(out)


_PREAMBLE = r"""\documentclass[letterpaper,10pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[letterpaper,top=0.45in,bottom=0.4in,left=0.5in,right=0.5in]{geometry}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{tabularx}
\urlstyle{same}
\pagestyle{empty}
\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}
\setlength{\parindent}{0pt}
\titleformat{\section}{\scshape\raggedright\large}{}{0em}{}[\vspace{-4pt}\titlerule]
\titlespacing*{\section}{0pt}{6pt}{4pt}
% Title (bold) and dates on one line; employer and place (italic) below
\newcommand{\role}[4]{\begin{tabular*}{\textwidth}[t]{@{}l@{\extracolsep{\fill}}r@{}}
  \textbf{#1} & #2 \\ \textit{#3} & \textit{#4} \\ \end{tabular*}\vspace{-4pt}}
% Project: name | stack on the left (wraps if long), links and context on the right
\newcommand{\proj}[2]{\begin{tabularx}{\textwidth}{@{}X@{\hspace{8pt}}r@{}}
  #1 & #2 \\ \end{tabularx}\vspace{-4pt}}
\setlist[itemize]{leftmargin=0.32in, label=\textbullet, itemsep=0.5pt, topsep=1pt, parsep=0pt, partopsep=0pt}
\begin{document}
"""


def _href(url: str, label: str = "") -> str:
    full = url if url.startswith("http") else "https://" + url
    shown = label or re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return "\\href{" + full.replace("%", "\\%").replace("#", "\\#") + "}{" + tex_escape(shown) + "}"


def render(resume: dict[str, Any], details: dict[str, str]) -> str:
    e = tex_escape
    name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip() or "Your Name"
    where = details.get("resume_location") or details.get("city", "")
    contact = [where, details.get("phone", ""), details.get("email", "")]
    def _same(a: str, b: str) -> bool:
        k = lambda u: re.sub(r"^https?://(www\.)?", "", u or "").rstrip("/").lower()
        return bool(a) and k(a) == k(b)
    website = "" if _same(details.get("website", ""), details.get("github", "")) else details.get("website", "")
    links = [(details.get("linkedin", ""), "LinkedIn"), (details.get("github", ""), "GitHub"), (website, "")]
    head = r" $\cdot$ ".join([e(c) for c in contact if c] +
                            [r"\underline{" + _href(u) + "}" for u, _ in links if u])
    plain = len(" . ".join([c for c in contact if c] + [u for u, _ in links if u]))
    size = r"\small" if plain <= 125 else r"\footnotesize"     # keep contact details on one line
    out = [_PREAMBLE, rf"\begin{{center}}{{\huge \textbf{{{e(name)}}}}}\\[2pt]{size} {head}\end{{center}}",
           r"\vspace{-10pt}"]

    def bullets(items: list[str]) -> str:
        return r"\begin{itemize}" + "".join(rf"\item {e(b)}" + "\n" for b in items) + r"\end{itemize}"

    if resume.get("experience"):
        out.append(r"\section{Experience}")
        for x in resume["experience"]:
            out.append(rf"\role{{{e(x.get('title', ''))}}}{{{e(x.get('dates', ''))}}}"
                       rf"{{{e(x.get('company', ''))}}}{{{e(x.get('location', ''))}}}")
            out.append(bullets(x["bullets"]))
    if resume.get("education"):
        out.append(r"\section{Education}")
        for ed in resume["education"]:
            out.append(rf"\role{{{e(ed.get('school', ''))}}}{{{e(ed.get('dates', ''))}}}"
                       rf"{{{e(ed.get('degree', ''))}}}{{{e(ed.get('location', ''))}}}")
            if ed.get("detail"):
                out.append(bullets([ed["detail"]]))
    if resume.get("projects"):
        out.append(r"\section{Projects}")
        for p in resume["projects"]:
            stack = ", ".join([t.strip() for t in (p.get("stack") or "").split(",") if t.strip()][:5])
            left = rf"\textbf{{{e(p.get('name', ''))}}}" + (rf" $|$ \emph{{{e(stack)}}}" if stack else "")
            link = p.get("link", "")
            context = _short(p.get("context", ""), 34)
            right = r" $\cdot$ ".join(x for x in ("[" + _href(link, "repo" if "github" in link else "link") + "]"
                                                 if link else "", e(context)) if x)
            out.append(rf"\proj{{{left}}}{{{right}}}")
            out.append(bullets(p["bullets"]))
    if resume.get("skills"):
        out.append(r"\section{Technical Skills}")
        out.append(r" \\ ".join(rf"\textbf{{{e(s['label'])}:}} {e(s['items'])}" for s in resume["skills"]))
    for x in resume.get("extras") or []:
        out.append(rf"\section{{{e(x.get('label', ''))}}}{e(x.get('text', ''))}")
    out.append(r"\end{document}")
    return "\n".join(out)


def _short(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;·-")
    return cut


def _pages(pdf: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 1


def build_pdf(resume: dict[str, Any], details: dict[str, str], out: Path) -> Path | None:
    with tempfile.TemporaryDirectory() as tmp:
        tex = Path(tmp) / "resume.tex"
        tex.write_text(render(resume, details))
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                           cwd=tmp, capture_output=True, text=True, timeout=60)
        pdf = Path(tmp) / "resume.pdf"
        if r.returncode != 0 or not pdf.exists():
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, out)
        shutil.copy2(tex, out.with_suffix(".tex"))
    return out


def _main_role(resume: dict[str, Any]) -> dict | None:
    """The job the resume rests on: the role with the most bullets in the draft."""
    roles = resume.get("experience") or []
    return max(roles, key=lambda x: len(x["bullets"])) if roles else None


def fit_one_page(resume: dict[str, Any], details: dict[str, str], out: Path) -> tuple[Path | None, list[str]]:
    """Build; while it runs past one page, cut the least important thing first:
    extra sections, a fourth skills line, project bullets past the first, the
    last project (keeping two), other roles down to two bullets then one, and
    the main job never below three. Returns (pdf, what was trimmed)."""
    trimmed: list[str] = []
    main = _main_role(resume)
    for _ in range(30):
        pdf = build_pdf(resume, details, out)
        if not pdf or _pages(pdf) <= 1:
            return pdf, trimmed
        projects = resume.get("projects") or []
        others = [x for x in resume.get("experience") or [] if x is not main]
        if resume.get("extras"):
            trimmed.append(f"section {resume['extras'].pop()['label']}")
        elif len(resume.get("skills") or []) > 3:
            trimmed.append(f"skills line {resume['skills'].pop()['label']}")
        elif [p for p in projects if len(p["bullets"]) > 2]:
            target = max(projects, key=lambda x: len(x["bullets"]))
            trimmed.append(target["bullets"].pop())
        elif len(projects) > 3:
            trimmed.append(f"project {projects.pop()['name']}")
        elif [x for x in others if len(x["bullets"]) > 2]:
            target = max(others, key=lambda x: len(x["bullets"]))
            trimmed.append(target["bullets"].pop())
        elif [p for p in projects if len(p["bullets"]) > 1]:
            target = [p for p in projects if len(p["bullets"]) > 1][-1]
            trimmed.append(target["bullets"].pop())
        elif len(projects) > 2:
            trimmed.append(f"project {projects.pop()['name']}")
        elif main and len(main["bullets"]) > 3:
            trimmed.append(main["bullets"].pop())
        elif [x for x in others if len(x["bullets"]) > 1]:
            target = [x for x in others if len(x["bullets"]) > 1][-1]
            trimmed.append(target["bullets"].pop())
        else:
            return pdf, trimmed
    return build_pdf(resume, details, out), trimmed


def write_resume(job: dict[str, Any], details: dict[str, str], folder: Path, filename: str,
                 private_dir: Path | None = None, client: Any = None) -> tuple[Path | None, dict[str, Any]]:
    """Draft → check against the profile → fit one page. Returns (pdf, info)."""
    import logging
    log = logging.getLogger(__name__)
    profile = load_profile(private_dir)
    from pipeline.ai.context import ensure_description
    job = ensure_description(job)
    the_plan, steps = None, []
    try:                                   # 1. read the job and decide what to say
        the_plan = plan(job, profile, private_dir, client)
        steps.append("planned")
    except Exception as e:                 # write without a plan rather than not at all
        log.warning(f"resume plan failed: {e}")
        steps.append(f"no plan ({e})"[:120])
    raw = draft(job, profile, private_dir, client, the_plan)      # 2. write it
    steps.append("written")
    try:                                   # 3. the hiring manager's pass
        raw = review(job, profile, the_plan, raw, private_dir, client)
        steps.append("reviewed")
    except Exception as e:
        log.warning(f"resume review failed: {e}")
        steps.append(f"not reviewed ({e})"[:120])
    clean, dropped = check(raw, profile)
    if not clean["experience"]:
        return None, {"error": "nothing in the draft passed the profile check", "dropped": dropped}
    pdf, trimmed = fit_one_page(clean, details, folder / filename)
    return pdf, {"note": clean.get("note", ""), "dropped": dropped, "trimmed": trimmed, "resume": clean,
                 "plan": the_plan, "steps": steps, "had_description": bool((job.get("description") or "").strip())}
