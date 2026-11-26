"""LinkedIn profile text, written from the career profile.

Recruiter sourcing is the second-largest way interviews start (Glassdoor,
2026), and recruiters find people by searching titles and skills. This
writes the three fields that search reads, headline, About and the skills
list, from the career profile only, aimed at the roles in config, and
checks every sentence of the About against the profile. You paste them
into LinkedIn yourself; nothing here touches your account.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.ai import client as ai
from pipeline.resume import writer

FILE = "linkedin_profile.json"

_SYSTEM = """You write a software engineer's LinkedIn profile text so recruiters searching
for the target roles find it and read it. Facts: only from the career profile;
never invent. Rules:
- headline: at most 200 characters. The target title first, then the 3-5
  strongest, most searched skills or achievements, separated by " | ". No
  "seeking", no emoji.
- about: 900-1300 characters, first person, plain sentences, no em dashes, no
  buzzwords ("passionate", "results-driven"). Paragraph 1: what the person
  builds and the stack, with one or two real numbers from the profile.
  Paragraph 2: the two or three things they are proudest of, concrete.
  Paragraph 3: what they are looking for (the target roles, location, start
  availability as the profile states it) and how to reach them.
- skills: 20-30 LinkedIn skill names (exact common spellings, e.g. "Spring
  Boot", "PostgreSQL"), most important first; only skills the profile shows
  in use.
- open_to: the 3-5 job titles to list under Open to Work, as recruiters
  search for them.
Reply with JSON only:
{"headline": "...", "about": "...", "skills": ["..."], "open_to": ["..."]}"""


def _path(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return (private_dir or applicant.PRIVATE_DIR) / FILE


def load(private_dir: Path | None = None) -> dict[str, Any] | None:
    p = _path(private_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def write(private_dir: Path | None = None, client: Any = None, roles: list[str] | None = None) -> dict[str, Any]:
    """Headline, About, skills and Open-to titles, saved to linkedin_profile.json."""
    profile = writer.load_profile(private_dir)
    if not profile.strip():
        raise RuntimeError("add your career profile first")
    if roles is None:
        try:
            from pipeline.utils.config import load_config
            roles = [str(r) for r in (load_config().get("profile") or {}).get("role_keywords") or []][:8]
        except Exception:
            roles = []
    prompt = "Target roles: " + (", ".join(roles) if roles else "software engineer (new grad / early career)")
    reply = ai.complete(writer._system(profile, _SYSTEM), prompt, purpose="linkedin profile text", max_tokens=2500,
                        private_dir=private_dir, client=client, model=writer._resume_model(private_dir))
    d = writer.parse_json(reply.text, private_dir, client)
    about = str(d.get("about", "")).replace("—", "-").strip()
    flagged = []
    for sentence in [s.strip() for s in about.replace("\n", " ").split(". ") if len(s.strip()) > 20]:
        ok, why = writer.supported(sentence, profile)
        if not ok:
            flagged.append({"line": sentence[:160], "why": why[:160]})
    data = {"headline": str(d.get("headline", ""))[:220].replace("—", "-"), "about": about[:2600],
            "skills": [str(x)[:60] for x in d.get("skills") or []][:40],
            "open_to": [str(x)[:80] for x in d.get("open_to") or []][:6],
            "flagged": flagged, "roles": roles, "cost": round(reply.cost, 4),
            "written_at": datetime.now().isoformat(timespec="minutes")}
    p = _path(private_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    return data
