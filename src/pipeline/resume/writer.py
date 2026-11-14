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


def _find(secs: dict[str, str], name: str, kind: str) -> str | None:
    """The profile text for an employer or project named in the resume."""
    n = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if not n:
        return None
    best = None
    for head, body in secs.items():
        if kind == "project" and not head.startswith("project:"):
            continue
        if kind == "role" and head.startswith("project:"):
            continue
        h = re.sub(r"[^a-z0-9]+", " ", head.lower().replace("project:", ""))
        if n in h or (len(h.split()) and h.split()[0] in n.split()):
            best = head + "\n" + body
            if n in h:
                break
    return best


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
        if not techy or lw in src_words:
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
        src = _find(secs, e.get("company", ""), "role")
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

_SYSTEM = """You write one applicant's resume for one job, the way a careful career coach would.

Use ONLY the applicant's career profile below. Every employer, title, date,
project, number, tool and claim must come from it, and a fact about one role
or project may only appear under that role or project. Follow the profile's
Rules section exactly (it overrides anything here). Never invent metrics. Write employer and project names exactly as the
profile's headings do (e.g. "IBM", "Sahara AI", "AccessMint").

Pick what this job cares about: the most relevant roles (keep the main
full-time job even if less relevant), 3–5 bullets for the main role, 1–3 for
others, 2–3 projects, 3–4 skill lines led by the job's stack (only skills the
profile has, at the level it states), education. Put the job's priorities
first and use its wording for things the applicant really did. Bold key
tools and metrics with **double asterisks**. No summary section unless the
Rules allow one. No em dashes. Keep it to one page: about 13–17 bullets in total.

Reply with JSON only:
{"experience": [{"company": "...", "title": "...", "location": "...", "dates": "...", "bullets": ["..."]}],
 "projects": [{"name": "...", "stack": "...", "link": "...", "bullets": ["..."]}],
 "skills": [{"label": "Languages", "items": "Java, Python, ..."}],
 "education": [{"school": "...", "degree": "...", "dates": "...", "location": "...", "detail": "Coursework: ..."}],
 "extras": [{"label": "Publications", "text": "..."}],
 "note": "<one sentence: what the job emphasises and what you led with>"}"""


def draft(job: dict[str, Any], profile: str, private_dir: Path | None = None, client: Any = None) -> dict[str, Any]:
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    system = [{"type": "text", "text": _SYSTEM},
              {"type": "text", "text": f"<career_profile>\n{profile[:60000]}\n</career_profile>",
               "cache_control": {"type": "ephemeral"}}]
    reply = ai.complete(system, job_block(job, limit=10000), purpose=f"resume: {job.get('company')} — "
                        f"{job.get('title', '')[:50]}", max_tokens=2500, private_dir=private_dir, client=client)
    m = re.search(r"\{.*\}", reply.text, re.S)
    if not m:
        raise ValueError("no resume in the reply")
    return json.loads(m.group(0))


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


_PREAMBLE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{tabularx}
\addtolength{\oddsidemargin}{-0.55in}
\addtolength{\evensidemargin}{-0.55in}
\addtolength{\textwidth}{1.1in}
\addtolength{\topmargin}{-.6in}
\addtolength{\textheight}{1.2in}
\urlstyle{same}
\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}
\titleformat{\section}{\scshape\raggedright\large}{}{0em}{}[\titlerule]
\titlespacing*{\section}{0pt}{9pt}{5pt}
\newcommand{\role}[4]{\vspace{1pt}\begin{tabular*}{\textwidth}[t]{l@{\extracolsep{\fill}}r}
  \textbf{#1} & #2 \\ \textit{\small #3} & \textit{\small #4} \\ \end{tabular*}\vspace{-3pt}}
\newcommand{\proj}[2]{\vspace{1pt}\begin{tabular*}{\textwidth}{l@{\extracolsep{\fill}}r}
  \small #1 & \small #2 \\ \end{tabular*}\vspace{-3pt}}
\setlist[itemize]{leftmargin=0.18in, itemsep=1pt, topsep=1pt, parsep=0pt, partopsep=0pt}
\setlength{\parindent}{0pt}
\begin{document}
"""


def _href(url: str) -> str:
    full = url if url.startswith("http") else "https://" + url
    shown = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return "\\href{" + full.replace("%", "\\%").replace("#", "\\#") + "}{" + tex_escape(shown) + "}"


def render(resume: dict[str, Any], details: dict[str, str]) -> str:
    e = tex_escape
    name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip() or "Your Name"
    contact = [details.get(k, "") for k in ("phone", "email", "city")]
    links = [details.get(k, "") for k in ("linkedin", "github", "website")]
    head = " $|$ ".join([e(c) for c in contact if c] + [_href(u) for u in links if u])
    out = [_PREAMBLE, rf"\begin{{center}}{{\Large \textbf{{{e(name)}}}}}\\ \vspace{{2pt}}\small {head}\end{{center}}",
           r"\vspace{-8pt}"]
    if resume.get("experience"):
        out.append(r"\section{Experience}")
        for x in resume["experience"]:
            out.append(rf"\role{{{e(x.get('company', ''))}}}{{{e(x.get('location', ''))}}}"
                       rf"{{{e(x.get('title', ''))}}}{{{e(x.get('dates', ''))}}}")
            out.append(r"\begin{itemize}" + "".join(rf"\item \small {e(b)}" + "\n" for b in x["bullets"]) + r"\end{itemize}")
    if resume.get("projects"):
        out.append(r"\section{Projects}")
        for p in resume["projects"]:
            left = rf"\textbf{{{e(p.get('name', ''))}}}" + (rf" $|$ \emph{{{e(p['stack'])}}}" if p.get("stack") else "")
            link = p.get("link", "")
            right = _href(link) if link else ""
            out.append(rf"\proj{{{left}}}{{{right}}}")
            out.append(r"\begin{itemize}" + "".join(rf"\item \small {e(b)}" + "\n" for b in p["bullets"]) + r"\end{itemize}")
    if resume.get("skills"):
        out.append(r"\section{Technical Skills}\small")
        out.append(r" \\ ".join(rf"\textbf{{{e(s['label'])}:}} {e(s['items'])}" for s in resume["skills"]))
    if resume.get("education"):
        out.append(r"\section{Education}")
        for ed in resume["education"]:
            out.append(rf"\role{{{e(ed.get('school', ''))}}}{{{e(ed.get('location', ''))}}}"
                       rf"{{{e(ed.get('degree', ''))}}}{{{e(ed.get('dates', ''))}}}")
            if ed.get("detail"):
                out.append(rf"\par\vspace{{3pt}}{{\small {e(ed['detail'])}}}\par\vspace{{2pt}}")
    for x in resume.get("extras") or []:
        out.append(rf"\section{{{e(x.get('label', ''))}}}\small {e(x.get('text', ''))}")
    out.append(r"\end{document}")
    return "\n".join(out)


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


def fit_one_page(resume: dict[str, Any], details: dict[str, str], out: Path) -> tuple[Path | None, list[str]]:
    """Build; while it runs past one page, drop a bullet from the longest
    project, then the longest non-first role. Returns (pdf, what was trimmed)."""
    trimmed: list[str] = []
    for _ in range(12):
        pdf = build_pdf(resume, details, out)
        if not pdf or _pages(pdf) <= 1:
            return pdf, trimmed
        pools = [p for p in resume.get("projects", []) if len(p["bullets"]) > 1] or \
                [x for x in resume.get("experience", [])[1:] if len(x["bullets"]) > 1] or \
                [x for x in resume.get("experience", [])[:1] if len(x["bullets"]) > 3]
        if pools:
            target = max(pools, key=lambda x: len(x["bullets"]))
            trimmed.append(target["bullets"].pop())
        elif len(resume.get("projects", [])) > 1:
            trimmed.append(f"project {resume['projects'].pop()['name']}")
        elif resume.get("extras"):
            trimmed.append(f"section {resume['extras'].pop()['label']}")
        else:
            return pdf, trimmed
    return build_pdf(resume, details, out), trimmed


def write_resume(job: dict[str, Any], details: dict[str, str], folder: Path, filename: str,
                 private_dir: Path | None = None, client: Any = None) -> tuple[Path | None, dict[str, Any]]:
    """Draft → check against the profile → fit one page. Returns (pdf, info)."""
    profile = load_profile(private_dir)
    raw = draft(job, profile, private_dir, client)
    clean, dropped = check(raw, profile)
    if not clean["experience"]:
        return None, {"error": "nothing in the draft passed the profile check", "dropped": dropped}
    pdf, trimmed = fit_one_page(clean, details, folder / filename)
    return pdf, {"note": clean.get("note", ""), "dropped": dropped, "trimmed": trimmed, "resume": clean}
