"""Hirebase: jobs from 500k+ company career pages, beyond the boards jobpipe knows.

Hirebase indexes career pages and ATS platforms directly (Greenhouse, Lever,
Workday, iCIMS and 80+ more) and tags each posting with fields like
visa_sponsored. jobpipe asks it, a few times a day, only for postings it
first saw since the last ask that match your titles and locations:

  - on a board jobpipe crawls (Greenhouse, Lever, Ashby, Workday…): the board
    is added to the crawler and read right away, so the job is applied to
    like any other;
  - anywhere else (iCIMS, SmartRecruiters, company sites): it shows up in
    Live jobs with its direct link, marked when Hirebase says it sponsors visas.

The free plan allows 500 jobs a month; asks are sized so the month's
allowance lasts the whole month. Key: data/private/hirebase_key or
HIREBASE_API_KEY (free at hirebase.org, Profile → API Key).
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

API = "https://api.hirebase.org/v2/jobs/search"
ESTIMATE = "https://api.hirebase.org/v2/jobs/estimate"      # free: doesn't count against the allowance
KEY_FILE = "hirebase_key"
SETTINGS = "hirebase.json"
DEFAULTS: dict[str, Any] = {"enabled": True, "every_hours": 8, "monthly_jobs": 480, "visa_only": False}

_BOARD_PLATFORM = {"workday": "workday", "smartrecruiters": "smartrecruiters", "workable": "workable",
                   "bamboohr": "bamboohr", "greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby"}
_STATES = {"NY": "New York", "CA": "California", "WA": "Washington", "MA": "Massachusetts", "TX": "Texas",
           "IL": "Illinois", "NJ": "New Jersey", "CO": "Colorado", "GA": "Georgia", "PA": "Pennsylvania",
           "VA": "Virginia", "NC": "North Carolina", "FL": "Florida", "OR": "Oregon", "DC": "District of Columbia"}


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def get_key(private_dir: Path | None = None) -> str:
    if os.environ.get("HIREBASE_API_KEY"):
        return os.environ["HIREBASE_API_KEY"].strip()
    try:
        return (_pdir(private_dir) / KEY_FILE).read_text().strip()
    except OSError:
        return ""


def save_key(key: str, private_dir: Path | None = None) -> None:
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", key):
        raise ValueError("That doesn't look like a Hirebase API key (Profile → API Key at hirebase.org).")
    p = _pdir(private_dir) / KEY_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key)
    os.chmod(p, 0o600)


def forget_key(private_dir: Path | None = None) -> None:
    (_pdir(private_dir) / KEY_FILE).unlink(missing_ok=True)


def state(private_dir: Path | None = None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads((_pdir(private_dir) / SETTINGS).read_text()))
    except (OSError, ValueError):
        pass
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if s.get("month") != month:                       # new month: fresh allowance
        s.update(month=month, used=0)
    return s


def save_state(s: dict[str, Any], private_dir: Path | None = None) -> None:
    p = _pdir(private_dir) / SETTINGS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2))
    os.chmod(p, 0o600)


def save_settings(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    s = state(private_dir)
    if "enabled" in values:
        s["enabled"] = bool(values["enabled"])
    if "visa_only" in values:
        s["visa_only"] = bool(values["visa_only"])
    if "every_hours" in values:
        s["every_hours"] = max(1, min(48, int(values["every_hours"])))
    if "monthly_jobs" in values:
        s["monthly_jobs"] = max(0, min(1_000_000, int(values["monthly_jobs"])))
    save_state(s, private_dir)
    return public(private_dir)


def public(private_dir: Path | None = None) -> dict[str, Any]:
    s = state(private_dir)
    return {**{k: s[k] for k in DEFAULTS}, "has_key": bool(get_key(private_dir)), "used": s.get("used", 0),
            "last": s.get("last") or {}, "next_due": s.get("next_due")}


def due(private_dir: Path | None = None, now: datetime | None = None) -> bool:
    s = state(private_dir)
    if not (s["enabled"] and get_key(private_dir)) or s.get("used", 0) >= s["monthly_jobs"]:
        return False
    nd = s.get("next_due")
    return not nd or (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(nd)


def per_ask(s: dict[str, Any], now: datetime | None = None) -> int:
    """Jobs to ask for now so the month's allowance lasts the month."""
    now = now or datetime.now(timezone.utc)
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    asks_left = max(1, math.ceil((nxt - now).total_seconds() / 3600 / s["every_hours"]))
    left = max(0, s["monthly_jobs"] - s.get("used", 0))
    return max(0, min(100, math.ceil(left / asks_left)))


# ── Asking ─────────────────────────────────────────────────────────────

def _geo(loc: str) -> dict[str, str] | None:
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts or parts[0].lower() == "remote":
        return None
    g = {"city": parts[0], "country": "United States"}
    if len(parts) > 1:
        g["region"] = _STATES.get(parts[1].upper(), parts[1])
    return g


def queries(config: dict, s: dict[str, Any], since: str | None, limit: int) -> list[dict]:
    """One ask for your cities, one for remote (US), sharing the limit."""
    filters = config.get("filters", {})
    titles = [t for t in filters.get("role_keywords", []) if t][:12] or ["Software Engineer"]
    places = config.get("profile", {}).get("locations") or config.get("locations") or filters.get("locations", [])
    geos = [g for g in (_geo(p) for p in places) if g][:5]
    remote = any("remote" in str(p).lower() for p in places) or not geos
    base: dict[str, Any] = {"job_titles": titles, "job_types": ["Full Time"], "hide_recruiting_agencies": "true",
                            "sort_by": "first_ingested", "sort_order": "desc", "page": 1}
    if s.get("visa_only"):
        base["visa"] = "true"
    if since:
        base["ingested_after"] = since
    else:
        base["days_ago"] = 2
    out = []
    share = max(1, limit // 2) if geos and remote else limit
    if geos:
        out.append({**base, "geo_locations": geos, "location_types": ["In-Person", "Hybrid"], "limit": share})
    if remote:
        out.append({**base, "location_types": ["Remote"], "geo_locations": [{"country": "United States"}],
                    "limit": max(1, limit - share) if geos else limit})
    return out


_REPOST_HOSTS = re.compile(r"(linkedin\.com|indeed\.com|glassdoor\.|ziprecruiter\.com|dice\.com|lensa\.com|"
                           r"talent\.com|simplyhired\.|monster\.com|careerbuilder\.com|jooble\.|adzuna\.|"
                           r"jobright\.ai|remotehunter\.|builtin\.com|wellfound\.com|otta\.com|jobot\.com)", re.I)
_REPOSTERS = {"remotehunter", "jobright", "jobright.ai", "lensa", "jobot", "dice", "talentify", "jobs via dice",
              "hiring cafe", "turing", "crossover", "braintrust", "mercor", "outlier", "dataannotation"}


def is_repost(h: dict) -> bool:
    """An agency or aggregator reposting someone else's job: no real employer
    form to apply on, and a middleman in between."""
    cd = h.get("company_data") or {}
    if h.get("recruiter_agency") or h.get("recruiter_ageny") or cd.get("is_recruiting_agency") or cd.get("is_3rd_party_agency"):
        return True
    link = h.get("application_link") or h.get("job_board_link") or ""
    if _REPOST_HOSTS.search(link):
        return True
    return re.sub(r"[^a-z. ]", "", (h.get("company_name") or "").lower()).strip() in _REPOSTERS


def to_job(h: dict) -> tuple[Any, tuple[str, str] | None]:
    """A Hirebase posting → (Job, crawlable board or None)."""
    from pipeline.models import ATSPlatform, Job
    from pipeline.scrapers.search_discover import _extract_slug
    link = h.get("application_link") or h.get("job_board_link") or ""
    board = _extract_slug(link) if link else None
    plat = _BOARD_PLATFORM.get(str(h.get("job_board") or "").lower(), "other")
    if board:
        plat = board[0]
    try:
        platform = ATSPlatform(plat)
    except ValueError:
        platform = ATSPlatform.OTHER
    locs = h.get("locations") or []
    location = h.get("location_raw") or "; ".join(
        ", ".join(x for x in (l.get("city"), l.get("region")) if x) for l in locs if isinstance(l, dict))
    if (h.get("location_type") or "").lower() == "remote" and "remote" not in location.lower():
        location = (location + "; Remote").strip("; ")
    posted = None
    try:
        posted = datetime.fromisoformat(str(h.get("date_posted"))[:10]) if h.get("date_posted") else None
    except ValueError:
        pass
    desc = "\n\n".join(x for x in (h.get("requirements_summary"), h.get("description")) if x)
    if h.get("visa_sponsored") is True:
        desc = "Visa sponsorship: offered (per Hirebase).\n\n" + desc
    job = Job(id=f"hb-{h.get('_id') or h.get('job_slug')}", title=h.get("job_title") or "",
              company=h.get("company_name") or "", company_slug=h.get("company_slug") or (board[1] if board else ""),
              location=location, url=link, platform=platform, description=desc[:20000], posted_at=posted)
    return job, board


def fetch_new(config: dict, passes: Callable[[str, str, str], bool] | None = None, private_dir: Path | None = None,
              client: httpx.Client | None = None, now: datetime | None = None) -> dict[str, Any]:
    """One ask. Returns {jobs (not on crawlable boards), boards, returned, error}."""
    s = state(private_dir)
    now = now or datetime.now(timezone.utc)
    key = get_key(private_dir)
    out: dict[str, Any] = {"jobs": [], "boards": set(), "returned": 0, "reposts": 0, "error": ""}
    limit = per_ask(s, now)
    if not key or limit <= 0:
        out["error"] = "no key" if not key else "this month's allowance is used up"
        return out
    since = (s.get("last") or {}).get("asked_at")
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        for body in queries(config, s, since, limit):
            r = client.post(API, json=body, headers={"x-api-key": key, "Content-Type": "application/json"})
            if r.status_code == 401:
                out["error"] = "Hirebase rejected the key"
                break
            if r.status_code == 402 or r.status_code == 429:
                out["error"] = f"Hirebase limit reached ({r.status_code})"
                break
            if r.status_code >= 400:
                out["error"] = f"Hirebase answered {r.status_code}: {r.text[:150]}"
                break
            hits = r.json().get("jobs") or []
            out["returned"] += len(hits)
            for h in hits:
                if is_repost(h):
                    out["reposts"] += 1
                    continue
                job, board = to_job(h)
                if passes is not None and not passes(job.title, job.location, job.description):
                    continue
                if board:
                    out["boards"].add(board)
                out["jobs"].append(job)
    except httpx.HTTPError as e:
        out["error"] = f"couldn't reach Hirebase ({type(e).__name__})"
    finally:
        if own:
            client.close()
    s["used"] = s.get("used", 0) + out["returned"]
    s["next_due"] = (now + timedelta(hours=s["every_hours"])).isoformat()
    if not out["error"]:
        s["last"] = {"asked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "returned": out["returned"], "reposts": out["reposts"],
                     "matching": len(out["jobs"]), "boards": len(out["boards"])}
    else:
        s["last"] = {**(s.get("last") or {}), "error": out["error"], "error_at": now.isoformat(timespec="seconds")}
    save_state(s, private_dir)
    return out
