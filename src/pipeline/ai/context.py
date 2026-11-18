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


def applicant_block(details: dict[str, str], resume_text: str, private_dir: Path | None = None) -> str:
    """Details + resume, plus the full career profile when you've saved one
    (so form answers and fit checks know everything, not just the resume)."""
    facts = "\n".join(f"{k}: {details[k]}" for k in _FACTS if details.get(k))
    out = (f"<applicant_details>\n{facts}\n</applicant_details>\n\n"
           f"<resume>\n{resume_text[:12000]}\n</resume>")
    try:
        from pipeline.resume.writer import has_profile, load_profile
        if has_profile(private_dir):
            out += ("\n\n<career_profile>\n" + load_profile(private_dir)[:40000] +
                    "\n</career_profile>\nThe career profile is the source of truth; follow its Rules.")
    except Exception:
        pass
    return out


def job_block(job: dict[str, Any], limit: int = 8000) -> str:
    jd = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    jd = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", jd)
    jd = " ".join(jd.split())[:limit]
    return (f"<job>\nCompany: {job.get('company', '')}\nTitle: {job.get('title', '')}\n"
            f"Location: {job.get('location', '')}\nDescription: {jd or '(not available)'}\n</job>")


def ensure_description(job: dict[str, Any]) -> dict[str, Any]:
    """The job with its description; fetches the posting page when the feed
    that found it didn't carry one (SimplifyJobs listings have none)."""
    if len(re.sub(r"<[^>]+>", " ", job.get("description") or "").split()) >= 60 or not job.get("url"):
        return job
    try:
        import httpx
        r = httpx.get(job["url"], timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (jobpipe resume writer)"})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return job
        html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", r.text)
        text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
        if len(text.split()) >= 60:
            return {**job, "description": text[:15000]}
    except Exception:
        pass
    return job


def load_resume_text(private_dir: Path | None = None) -> str:
    from pipeline import applicant
    from pipeline.ai.client import _pdir
    path = _pdir(private_dir) / applicant.RESUME_FILE
    try:
        return applicant.resume_text(path.read_bytes()) if path.exists() else ""
    except Exception:
        return ""
