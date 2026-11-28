"""Interview odds: how likely one application is to turn into an interview,
as a number you can argue with, and why.

Fit score says how well the job matches you. Odds say how likely a good
application is to be read and answered, which is a different question:
the same role is a different bet at 2 hours old and at 2 weeks old, at a
40-person startup and at a 40,000-person enterprise, asking for 2-4 years
or for new grads (who arrive by the thousand). The feed and auto-apply
rank by odds so the day's ten applications go where they count.

Everything here is from the posting and public signals, no AI, so it
costs nothing and is the same every time.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pipeline.utils.text import extract_experience_years

# ATS platform as a size proxy: small companies read every application
_SMALL = {"ashby", "lever", "workable", "rippling", "wellfound"}
_LARGE = {"workday", "taleo", "successfactors", "icims", "smartrecruiters", "eightfold"}
_MASS = re.compile(r"\b(new grad|new graduate|university grad|early career|entry[- ]level|graduate program|"
                   r"campus|class of 20\d\d)\b", re.I)
_LEAD = re.compile(r"\b(lead|senior|sr\.?|staff|principal|head|manager)\b", re.I)
_II = re.compile(r"\b(ii|2|iii|3|mid[- ]level|software engineer, (?:product|platform|infrastructure))\b", re.I)


def _hours(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts)[:19])
    except ValueError:
        return None
    return max(0.0, (now - t).total_seconds() / 3600)


def odds(job: dict[str, Any], years: float = 3.0, now: datetime | None = None) -> dict[str, Any]:
    """{"score": 0-100, "label": "good" | "fair" | "long shot", "why": [...]}."""
    now = now or datetime.now()
    score = 50.0
    why: list[str] = []

    # Age: the first day decides most of it (LinkedIn: 4x reply rate inside the
    # first minutes; TalentWorks: odds fall by the day)
    posted = _hours(job.get("posted_at"), now)
    seen = _hours(job.get("first_seen_at"), now)
    age = max([h for h in (posted, seen) if h is not None], default=None)
    if job.get("baseline") and posted is None:
        age = None                                   # we never saw it open
    if age is None:
        why.append("age unknown")
    elif age <= 6:
        score += 22
        why.append(f"opened {int(age)}h ago" if age >= 1 else "opened under an hour ago")
    elif age <= 24:
        score += 14
        why.append(f"opened {int(age)}h ago")
    elif age <= 72:
        score += 4
        why.append(f"{int(age // 24)} day{'s' if age >= 48 else ''} old")
    elif age <= 24 * 7:
        score -= 6
        why.append(f"{int(age // 24)} days old")
    else:
        score -= 16
        why.append(f"{int(age // 24)} days old: hundreds ahead of you")

    # Company size, from the board it runs
    platform = str(job.get("platform") or job.get("source") or "").lower()
    if platform in _SMALL:
        score += 10
        why.append("small company: people read applications")
    elif platform in _LARGE:
        score -= 6
        why.append("large company: screened by software first")

    # Level: a posting asking for what you have beats a new-grad posting with
    # thousands of applicants; asking for well over it is a long shot
    title = str(job.get("title") or "")
    desc = str(job.get("description") or "")
    exp = extract_experience_years(desc)
    if exp:
        lo, hi = exp
        if lo <= years <= hi + 1 and lo >= 1:
            score += 14
            why.append(f"asks {lo}-{hi} years: you have {years:g}")
        elif lo > years + 1:
            score -= 20
            why.append(f"asks {lo}+ years")
        elif lo == 0 and _MASS.search(title + " " + desc[:400]):
            score -= 8 if platform not in _SMALL else 0
            why.append("new-grad posting: crowded")
    elif _MASS.search(title):
        score -= 8 if platform not in _SMALL else 0
        why.append("new-grad posting: crowded")
    elif _II.search(title):
        score += 8
        why.append("mid-level title: fewer applicants, your level")
    if _LEAD.search(title) and years < 6:
        score -= 12
        why.append("lead/senior title: above your years")

    # Sponsorship: the hard stop and the proven path
    if job.get("jd_blocked") or str(job.get("sponsorship") or "") == "blocked":
        score -= 50
        why.append("posting says no sponsorship")
    else:
        filings = int(job.get("h1b_filings_count") or 0)
        if filings >= 10:
            score += 8
            why.append(f"sponsors ({filings} recent H-1B filings)")
        elif filings > 0:
            score += 3
            why.append("has sponsored before")

    # A PERM labour-certification ad is not a real opening
    if re.search(r"#LI-DNI|labor certification|PERM\b", desc) or re.search(r"\b\d+ years of experience in each of\b", desc, re.I):
        score -= 45
        why.append("looks like a PERM ad: not a real opening")

    # Remote draws the whole country; a city posting draws the city
    loc = str(job.get("location") or "").lower()
    if "remote" in loc and not re.search(r"new york|san francisco|bay area|nyc", loc):
        score -= 5
        why.append("remote: nationwide competition")

    # Fit, lightly: odds is about being read, fit about being wanted
    fit = float(job.get("fit_score") or 0)
    if fit >= 85:
        score += 6
    elif fit < 60:
        score -= 6

    score = max(0.0, min(100.0, score))
    label = "good" if score >= 68 else "fair" if score >= 45 else "long shot"
    return {"score": round(score), "label": label, "why": why[:5]}
