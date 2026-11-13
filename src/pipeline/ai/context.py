"""What Claude is told about you and the job — shared by every AI feature."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Saved details that help answer questions (never contact info beyond links,
# never demographics — those aren't Claude's to decide)
_FACTS = ["first_name", "last_name", "city", "country", "citizenship", "authorized_us",
          "sponsorship_now", "sponsorship_future", "visa_status", "school", "degree", "major",
          "graduation", "current_employer", "past_employers", "relocate", "start_date",
          "linkedin", "github", "website"]


def applicant_block(details: dict[str, str], resume_text: str) -> str:
    facts = "\n".join(f"{k}: {details[k]}" for k in _FACTS if details.get(k))
    return (f"<applicant_details>\n{facts}\n</applicant_details>\n\n"
            f"<resume>\n{resume_text[:12000]}\n</resume>")


def job_block(job: dict[str, Any], limit: int = 8000) -> str:
    jd = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    jd = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", jd)
    jd = " ".join(jd.split())[:limit]
    return (f"<job>\nCompany: {job.get('company', '')}\nTitle: {job.get('title', '')}\n"
            f"Location: {job.get('location', '')}\nDescription: {jd or '(not available)'}\n</job>")


def load_resume_text(private_dir: Path | None = None) -> str:
    from pipeline import applicant
    from pipeline.ai.client import _pdir
    path = _pdir(private_dir) / applicant.RESUME_FILE
    try:
        return applicant.resume_text(path.read_bytes()) if path.exists() else ""
    except Exception:
        return ""
