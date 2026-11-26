"""Reach out about a job: who to contact, their email, what to say, when to follow up.

What the research says, and what this follows:
  - Write to the hiring manager (usually one level above the role, on the
    team the posting describes). Cold notes to the hiring manager help; at
    big companies and startups, cold notes to recruiters tend not to.
  - A short sequence gets about twice the replies of a single email, and most
    replies come from the follow-ups. So each contact gets a first email, a
    LinkedIn note and two follow-ups, reminded on days 4 and 9.
  - Emails: you find the person (LinkedIn links are prepared for you), then
    an email comes from Hunter (optional key; it says how sure it is) or a
    pattern guess, which is always marked unverified.

Nothing is sent from your accounts. Drafts open in your mail app or Gmail,
and you press Send; then you mark it sent here so follow-ups are scheduled.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

FILE = "outreach.json"
HUNTER_FILE = "hunter_key"
APOLLO_FILE = "apollo_key"
FOLLOW_UP_DAYS = (4, 9)          # after the first message


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load(private_dir: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((_pdir(private_dir) / FILE).read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any], private_dir: Path | None) -> None:
    p = _pdir(private_dir) / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def record(job: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    data = load(private_dir)
    rec = data.get(job["id"]) or {"job_id": job["id"], "company": job.get("company", ""),
                                  "title": job.get("title", ""), "url": job.get("url", ""),
                                  "created_at": _now(), "plan": None, "contacts": [],
                                  "domain": guess_domain(job.get("company", ""))}
    return rec


def _put(rec: dict[str, Any], private_dir: Path | None) -> dict[str, Any]:
    data = load(private_dir)
    data[rec["job_id"]] = rec
    _save(data, private_dir)
    return rec


# ── Who to contact ─────────────────────────────────────────────────────

def search_links(company: str, titles: list[str], team: str = "") -> list[dict[str, str]]:
    """LinkedIn people search and a Google search of LinkedIn profiles, per title."""
    out = []
    for t in titles[:4]:
        kw = f"{t} {company}"
        g = f'site:linkedin.com/in "{company}" "{t}"' + (f' "{team}"' if team else "")
        out.append({"title": t,
                    "linkedin": "https://www.linkedin.com/search/results/people/?keywords=" + quote(kw),
                    "google": "https://www.google.com/search?q=" + quote_plus(g)})
    return out


_FALLBACK_TITLES = ["Engineering Manager", "Senior Engineering Manager", "Director of Engineering"]

_PLAN_SYSTEM = """You help a job seeker decide whom to contact about one opening.
Read the posting and name the team it sits on and the 2–4 job titles most
likely to be the hiring manager or the person one level above the role on that
team (e.g. "Engineering Manager, Payments Infrastructure"). Prefer managers
over recruiters; add a recruiter title last only if the company is small or
the posting names a recruiter. Reply with JSON only:
{"team": "...", "titles": ["..."], "why": "<one sentence>", "angle": "<the one thing from the applicant's background most relevant to this team>"}"""


def plan(job: dict[str, Any], private_dir: Path | None = None, client: Any = None) -> dict[str, Any]:
    """Whom to look for. Uses AI when available, else generic manager titles."""
    from pipeline.ai import client as ai
    rec = record(job, private_dir)
    result = {"team": "", "titles": list(_FALLBACK_TITLES), "why": "Generic manager titles (no AI).",
              "angle": "", "by": "rules"}
    if ai.status(private_dir)[0] and (job.get("description") or "").strip():
        from pipeline.ai.context import job_block
        from pipeline.resume.writer import load_profile
        prof = load_profile(private_dir)[:20000]
        system = [{"type": "text", "text": _PLAN_SYSTEM},
                  {"type": "text", "text": f"<applicant>\n{prof}\n</applicant>", "cache_control": {"type": "ephemeral"}}]
        try:
            reply = ai.complete(system, job_block(job, 8000), purpose=f"outreach plan: {job.get('company')}",
                                max_tokens=300, private_dir=private_dir, client=client, model="haiku")
            m = re.search(r"\{.*\}", reply.text, re.S)
            data = json.loads(m.group(0)) if m else {}
            titles = [str(t)[:80] for t in data.get("titles") or [] if str(t).strip()][:4]
            if titles:
                result = {"team": str(data.get("team", ""))[:80], "titles": titles,
                          "why": str(data.get("why", ""))[:300], "angle": str(data.get("angle", ""))[:300],
                          "by": "ai"}
        except Exception as e:                     # keep the fallback
            result["why"] += f" (AI failed: {str(e)[:120]})"
    result["links"] = search_links(job.get("company", ""), result["titles"], result["team"])
    rec["plan"] = result
    return _put(rec, private_dir)


_FIND_SYSTEM = """You find the people a job seeker should contact about one opening,
using web search over public pages (LinkedIn public profiles, company team
pages, GitHub, conference bios, press). Never invent a person. Rules:
- The hiring manager is almost always the person one level above the role
  on the team the posting names; at a company under ~50 people it is often a
  founder or the CTO/Head of Engineering.
- Search 3-5 times at most. Useful queries: site:linkedin.com/in "<Company>"
  "<title>"; "<Company>" engineering team; "<Company>" "<team>" manager.
- Return at most 4 people, best first. For each give the exact name as the
  page shows it, their title, the public URL you saw them on, what the page
  said (one line of evidence), whether they are likely the hiring manager /
  a recruiter / an engineer on the team, and a confidence: "high" if the
  page shows them leading exactly that team now, "medium" if the team or
  current role is inferred, "low" if only the company matches.
- Skip people who left the company, and anyone whose page you did not see.
Reply with JSON only:
{"people": [{"name": "...", "title": "...", "url": "https://...", "evidence": "...",
             "kind": "manager|recruiter|engineer", "confidence": "high|medium|low"}],
 "note": "<one sentence on how sure you are overall, or what you could not find>"}"""


def find_people(job: dict[str, Any], private_dir: Path | None = None, client: Any = None,
                searches: int = 5) -> dict[str, Any]:
    """Named people to contact, found by web search over public pages (no
    LinkedIn login, nothing scraped with your account). Needs a plan first;
    saves the candidates on the outreach record for you to accept or ignore."""
    from pipeline.ai import client as ai
    rec = record(job, private_dir)
    the_plan = rec.get("plan") or plan(job, private_dir, client).get("plan") or {}
    ready, why = ai.status(private_dir)
    if not ready:
        raise RuntimeError(why)
    prompt = (f"Company: {job.get('company', '')}\nRole: {job.get('title', '')}\n"
              f"Location: {job.get('location', '')}\nPosting URL: {job.get('url', '')}\n"
              f"Team (from the posting): {the_plan.get('team') or 'not stated'}\n"
              f"Titles to look for: {', '.join(the_plan.get('titles') or _FALLBACK_TITLES)}\n"
              f"Company website domain, if known: {rec.get('domain') or 'unknown'}\n\n"
              f"Posting excerpt:\n{(job.get('description') or '')[:2500]}")
    reply = ai.complete(_FIND_SYSTEM, prompt, purpose=f"find people: {job.get('company')}", max_tokens=5000,
                        private_dir=private_dir, client=client, model="sonnet", web_search=searches)
    m = re.search(r"\{.*\}", reply.text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    people = []
    for x in data.get("people") or []:
        name = " ".join(str(x.get("name", "")).split())[:80]
        url = str(x.get("url", "")).strip()[:300]
        if not name or not url.startswith("http"):
            continue
        people.append({"name": name, "title": str(x.get("title", ""))[:120], "url": url,
                       "evidence": str(x.get("evidence", ""))[:300],
                       "kind": x.get("kind") if x.get("kind") in ("manager", "recruiter", "engineer") else "manager",
                       "confidence": x.get("confidence") if x.get("confidence") in ("high", "medium", "low") else "low"})
    rec["candidates"] = {"people": people[:4], "note": str(data.get("note", ""))[:300],
                         "found_at": _now(), "cost": round(reply.cost, 4)}
    return _put(rec, private_dir)


def accept_candidate(job: dict[str, Any], index: int, private_dir: Path | None = None) -> dict[str, Any]:
    """Turn a found person into a contact (you decided they're the one)."""
    rec = record(job, private_dir)
    people = (rec.get("candidates") or {}).get("people") or []
    if not 0 <= index < len(people):
        raise KeyError("no such person")
    x = people[index]
    linkedin = x["url"] if "linkedin.com" in x["url"] else ""
    return add_contact(job, {"name": x["name"], "title": x["title"], "linkedin": linkedin, "kind": x["kind"]},
                       private_dir)


# ── Contacts ───────────────────────────────────────────────────────────

def add_contact(job: dict[str, Any], values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    rec = record(job, private_dir)
    name = " ".join(str(values.get("name", "")).split())[:80]
    if not name:
        raise ValueError("Give the person's name.")
    email = str(values.get("email", "")).strip()[:120]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
        raise ValueError("That email doesn't look right.")
    linkedin = str(values.get("linkedin", "")).strip()[:200]
    if linkedin and not re.match(r"^(https?://)?([a-z]+\.)?linkedin\.com/", linkedin, re.I):
        raise ValueError("The LinkedIn link should be a linkedin.com address.")
    rec["contacts"].append({"id": secrets.token_hex(4), "name": name,
                            "title": str(values.get("title", ""))[:120], "linkedin": linkedin,
                            "email": email, "email_status": "yours" if email else "",
                            "email_note": "", "kind": values.get("kind") if values.get("kind") in
                            ("manager", "recruiter", "engineer", "other") else "manager",
                            "drafts": None, "sent_at": None, "followups_sent": [], "replied_at": None,
                            "added_at": _now()})
    if values.get("domain"):
        rec["domain"] = clean_domain(str(values["domain"]))
    return _put(rec, private_dir)


def _contact(rec: dict[str, Any], cid: str) -> dict[str, Any]:
    c = next((c for c in rec["contacts"] if c["id"] == cid), None)
    if not c:
        raise KeyError("no such contact")
    return c


def update_contact(job: dict[str, Any], cid: str, action: str, private_dir: Path | None = None,
                   values: dict[str, Any] | None = None) -> dict[str, Any]:
    """action: sent | followup | replied | unsent | delete | edit."""
    rec = record(job, private_dir)
    c = _contact(rec, cid)
    if action == "sent":
        c["sent_at"] = c["sent_at"] or _now()
    elif action == "followup":
        c["followups_sent"].append(_now())
    elif action == "replied":
        c["replied_at"] = _now()
    elif action == "unsent":
        c["sent_at"], c["followups_sent"], c["replied_at"] = None, [], None
    elif action == "delete":
        rec["contacts"].remove(c)
    elif action == "edit":
        v = values or {}
        for k in ("name", "title", "linkedin"):
            if k in v:
                c[k] = str(v[k]).strip()[:200]
        if "email" in v:
            c["email"] = str(v["email"]).strip()[:120]
            c["email_status"], c["email_note"] = ("yours", "") if c["email"] else ("", "")
    else:
        raise ValueError(f"unknown action {action}")
    return _put(rec, private_dir)


# ── Email addresses ────────────────────────────────────────────────────

_SUFFIXES = r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|hq|group|holdings)\b\.?"


def clean_domain(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s.removeprefix("www.")


def guess_domain(company: str) -> str:
    """Best guess at a company's email domain; you can correct it."""
    base = unicodedata.normalize("NFKD", company).encode("ascii", "ignore").decode().lower()
    base = re.sub(_SUFFIXES, "", base)
    base = re.sub(r"[^a-z0-9]", "", base)
    return f"{base}.com" if base else ""


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def name_parts(name: str) -> tuple[str, str]:
    words = [w for w in re.split(r"\s+", name.strip()) if w and not re.fullmatch(r"(dr|mr|ms|mrs)\.?", w, re.I)]
    words = [w for w in words if not re.fullmatch(r"(jr|sr|ii|iii|phd|mba)\.?,?", w, re.I)]
    if not words:
        return "", ""
    return _ascii(words[0]), _ascii(words[-1]) if len(words) > 1 else ""


# Most common corporate patterns first
PATTERNS = ["{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}", "{last}.{first}",
            "{first}{l}", "{f}.{last}", "{last}"]


def from_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    if ("{last}" in pattern or "{l}" in pattern) and not last:
        return ""
    local = pattern.format(first=first, last=last, f=first[:1], l=last[:1])
    return f"{local}@{domain}" if local and domain else ""


def guesses(name: str, domain: str) -> list[str]:
    first, last = name_parts(name)
    out = []
    for p in PATTERNS:
        e = from_pattern(p, first, last, domain)
        if e and e not in out:
            out.append(e)
    return out


def hunter_key(private_dir: Path | None = None) -> str:
    env = os.environ.get("HUNTER_API_KEY", "").strip()
    if env:
        return env
    try:
        return (_pdir(private_dir) / HUNTER_FILE).read_text().strip()
    except OSError:
        return ""


def save_hunter_key(key: str, private_dir: Path | None = None) -> None:
    key = key.strip()
    p = _pdir(private_dir) / HUNTER_FILE
    if not key:
        p.unlink(missing_ok=True)
        return
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", key):
        raise ValueError("That doesn't look like a Hunter API key.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key)
    os.chmod(p, 0o600)


def apollo_key(private_dir: Path | None = None) -> str:
    env = os.environ.get("APOLLO_API_KEY", "").strip()
    if env:
        return env
    try:
        return (_pdir(private_dir) / APOLLO_FILE).read_text().strip()
    except OSError:
        return ""


def save_apollo_key(key: str, private_dir: Path | None = None) -> None:
    key = key.strip()
    p = _pdir(private_dir) / APOLLO_FILE
    if not key:
        p.unlink(missing_ok=True)
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
        raise ValueError("That doesn't look like an Apollo API key.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key)
    os.chmod(p, 0o600)


_APOLLO = "https://api.apollo.io/api/v1"


def apollo_match(first: str, last: str, domain: str, company: str, key: str, client: httpx.Client) -> tuple[str, str]:
    """Apollo's people-match for one person: (email, note). Free keys get a
    masked address back (email_not_unlocked@…), which counts as not found."""
    try:
        r = client.post(f"{_APOLLO}/people/match", headers={"x-api-key": key, "Content-Type": "application/json"},
                        json={"first_name": first, "last_name": last, "domain": domain,
                              "organization_name": company, "reveal_personal_emails": False})
    except httpx.HTTPError as e:
        return "", f"Couldn't reach Apollo ({type(e).__name__})."
    if r.status_code in (401, 403):
        return "", "Apollo rejected the key."
    if r.status_code == 429:
        return "", "Apollo's credits are used up for now."
    if r.status_code != 200:
        return "", f"Apollo answered {r.status_code}."
    person = (r.json() or {}).get("person") or {}
    email = str(person.get("email") or "").strip()
    if not email or "not_unlocked" in email or email.startswith("email_") or "@" not in email:
        return "", "Apollo has no revealed address for them (a free key returns masked results)."
    status = str(person.get("email_status") or "")
    return email, f"Apollo{', ' + status if status else ''}"


def hunter_verify(email: str, key: str, client: httpx.Client) -> str:
    """Hunter's verdict on an address: valid / accept_all / invalid / unknown
    (half a credit)."""
    try:
        r = client.get(f"{_HUNTER}/email-verifier", params={"email": email, "api_key": key})
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    return str((r.json().get("data") or {}).get("status") or "")


_HUNTER = "https://api.hunter.io/v2"
_PAT_MAP = {"{first}": "{first}", "{last}": "{last}", "{f}": "{f}", "{l}": "{l}"}


def hunter_pattern(domain: str, key: str, client: httpx.Client) -> str:
    """The company's pattern from Hunter's domain search (e.g. "{first}.{last}")."""
    r = client.get(f"{_HUNTER}/domain-search", params={"domain": domain, "limit": 1, "api_key": key})
    if r.status_code != 200:
        return ""
    return str((r.json().get("data") or {}).get("pattern") or "")


def find_email(job: dict[str, Any], cid: str, private_dir: Path | None = None,
               client: httpx.Client | None = None, domain: str | None = None) -> dict[str, Any]:
    """Fill a contact's email, climbing a ladder until one is trustworthy:
    1. Hunter's finder (stops here when it is verified or 70%+ confident);
    2. Apollo's people match, if you saved a key;
    3. the company's email pattern from Hunter, applied to the name;
    4. the most common pattern.
    Any guess from steps 3-4 is checked with Hunter's verifier when a key is
    there; the next-best forms are kept as alternates so you can decide.
    Guesses that could not be verified are marked unverified."""
    rec = record(job, private_dir)
    if domain:
        rec["domain"] = clean_domain(domain)
    dom = rec.get("domain") or ""
    c = _contact(rec, cid)
    if not dom:
        raise ValueError("Set the company's email domain first (e.g. stripe.com).")
    first, last = name_parts(c["name"])
    key = hunter_key(private_dir)
    akey = apollo_key(private_dir)
    http = client or httpx.Client(timeout=20)
    found, status, notes, alternates = "", "", [], []

    def verify(addr: str) -> str:
        return hunter_verify(addr, key, http) if key and addr else ""

    # 1. Hunter finder
    if key:
        try:
            r = http.get(f"{_HUNTER}/email-finder", params={"domain": dom, "first_name": first,
                                                             "last_name": last, "api_key": key})
            if r.status_code == 200:
                d = r.json().get("data") or {}
                if d.get("email"):
                    v = (d.get("verification") or {}).get("status") or ""
                    score = int(d.get("score") or 0)
                    if v == "valid" or score >= 70:
                        found, status = d["email"], "verified" if v == "valid" else "found"
                        notes.append(f"Hunter, {score}% confidence" + (f", {v}" if v else ""))
                    else:
                        alternates.append(d["email"])
                        notes.append(f"Hunter's best guess was only {score}% confident")
            elif r.status_code in (401, 403):
                notes.append("Hunter rejected the key")
            elif r.status_code == 429:
                notes.append("Hunter's free credits are used up for now")
        except httpx.HTTPError as e:
            notes.append(f"Couldn't reach Hunter ({type(e).__name__})")
    # 2. Apollo
    if not found and akey:
        email, note = apollo_match(first, last, dom, rec.get("company", ""), akey, http)
        if email:
            v = verify(email)
            found, status = email, "verified" if v == "valid" else "found"
            notes.append(note + (f"; Hunter says {v}" if v else ""))
        else:
            notes.append(note.rstrip("."))
    # 3. the company's pattern, 4. the common pattern — each checked when possible
    if not found:
        candidates: list[tuple[str, str]] = []
        if key:
            try:
                pat = hunter_pattern(dom, key, http)
                if pat:
                    built = from_pattern(pat, first, last, dom)
                    if built:
                        candidates.append((built, f"built from {dom}'s pattern on Hunter ({pat})"))
            except httpx.HTTPError:
                pass
        for g in guesses(c["name"], dom):
            if g not in [x for x, _ in candidates]:
                candidates.append((g, "common pattern"))
        for addr, how in candidates[:2]:          # at most one credit of verification
            v = verify(addr)
            if v == "valid":
                found, status = addr, "verified"
                notes.append(f"{how}; Hunter verified it")
                break
            if v == "invalid":
                notes.append(f"{addr} is invalid per Hunter")
                continue
            if not found:
                found, status = addr, "guessed"
                notes.append(f"{how}; not verified" + (f" (Hunter: {v})" if v else ""))
                break
        alternates += [a for a, _ in candidates if a != found][:3]
    c["email"], c["email_status"] = found, status if found else ""
    c["email_note"] = ("; ".join(n for n in notes if n) or "Couldn't form a guess from that name.").strip()
    c["email_alternates"] = [a for a in dict.fromkeys(alternates) if a and a != found][:3]
    return _put(rec, private_dir)


# ── Drafts ─────────────────────────────────────────────────────────────

_DRAFT_SYSTEM = """You write short, human outreach for one job seeker to one person at a company.

Use ONLY facts from the applicant's career profile; follow its Rules section
exactly (no em dashes, never claim what the Rules forbid, and do not mention
AI coding tools such as Claude Code or Cursor). Never invent numbers.

The first email: under 120 words. Subject line under 8 words, specific.
Open with why them (their team's work from the posting), give ONE concrete,
relevant thing the applicant built with its result, then one clear ask (a
15-minute chat, or a referral/pointer to the right person). Say they applied
if the applicant has. No flattery, no "I hope this finds you well", no
desperation, no attachments mentioned unless asked.

LinkedIn connection note: under 280 characters, same idea, no link.
Follow-up 1 (day 4): 2–3 sentences, adds one new relevant detail, same ask.
Follow-up 2 (day 9): 1–2 sentences, polite close, easy out.

Adjust for the person's role: a hiring manager hears about the work; a
recruiter hears fit plus logistics (work authorization only if the profile
says how to describe it); an engineer is asked for perspective on the team.

Reply with JSON only:
{"subject": "...", "email": "...", "linkedin": "...", "followup1": "...", "followup2": "..."}"""


def _dash_free(s: str) -> str:
    return re.sub(r"\s*[—–]\s*", ", ", s).replace(" ,", ",")


def draft(job: dict[str, Any], cid: str, private_dir: Path | None = None, client: Any = None,
          applied: bool | None = None) -> dict[str, Any]:
    from pipeline import applicant
    from pipeline.ai import client as ai
    from pipeline.ai.context import job_block
    from pipeline.resume.writer import evidence, load_profile, supported
    rec = record(job, private_dir)
    c = _contact(rec, cid)
    prof = load_profile(private_dir)
    if not prof.strip():
        from pipeline.ai.context import load_resume_text
        prof = load_resume_text(private_dir)
    if not prof.strip():
        raise ValueError("Add your career profile (or upload a resume) first; drafts are written from it.")
    details = applicant.load(private_dir)
    me = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip()
    applied = (job.get("status") == "applied") if applied is None else applied
    system = [{"type": "text", "text": _DRAFT_SYSTEM},
              {"type": "text", "text": f"<career_profile>\n{prof[:40000]}\n</career_profile>",
               "cache_control": {"type": "ephemeral"}}]
    angle = (rec.get("plan") or {}).get("angle", "")
    prompt = (job_block(job, 6000) + f"\n<recipient>\nName: {c['name']}\nTitle: {c['title']}\nRole: {c['kind']}\n"
              f"</recipient>\n<sender>{me or 'the applicant'}; has {'already' if applied else 'not yet'} applied."
              + (f" Suggested angle: {angle}" if angle else "") + "</sender>")
    reply = ai.complete(system, prompt, purpose=f"outreach: {job.get('company')} / {c['name']}",
                        max_tokens=900, private_dir=private_dir, client=client)
    m = re.search(r"\{.*\}", reply.text, re.S)
    if not m:
        raise ValueError("The draft came back empty; try again.")
    d = json.loads(m.group(0))
    out = {k: _dash_free(str(d.get(k, "")).strip()) for k in ("subject", "email", "linkedin", "followup1", "followup2")}
    if len(out["linkedin"]) > 300:
        out["linkedin"] = out["linkedin"][:297].rsplit(" ", 1)[0] + "…"
    # Same fact check as the resume: numbers and technologies must be in the profile (or the posting)
    proof = evidence(prof) + "\n" + (job.get("description") or "") + "\n" + job.get("company", "") + " " + c["name"]
    warnings = []
    for k in ("email", "linkedin", "followup1", "followup2"):
        for sentence in re.split(r"(?<=[.!?])\s+", out[k]):
            ok, why = supported(sentence, proof)
            if not ok:
                warnings.append(f"{k}: {why}: “{sentence[:100]}”")
    out["warnings"], out["written_at"] = warnings, _now()
    c["drafts"] = out
    return _put(rec, private_dir)


def compose_links(rec: dict[str, Any], c: dict[str, Any], which: str = "email") -> dict[str, str]:
    """mailto: and Gmail compose links that open the draft for you to send."""
    d = c.get("drafts") or {}
    body = d.get(which, "")
    subject = d.get("subject", "") if which == "email" else f"Re: {d.get('subject', '')}"
    to = c.get("email", "")
    return {"mailto": f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}",
            "gmail": "https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(to) + "&su=" + quote(subject)
                     + "&body=" + quote(body)}


# ── Follow-ups ─────────────────────────────────────────────────────────

def send_window(now: datetime | None = None) -> dict[str, Any]:
    """When a note to a hiring manager is most likely to be read: Tuesday to
    Thursday, 8-10 am in their time zone (recruiter-activity data, 2026).
    Returns whether now is inside that window and, if not, the next one."""
    now = now or datetime.now()
    good_days = (1, 2, 3)                       # Tue, Wed, Thu
    if now.weekday() in good_days and 8 <= now.hour < 11:
        return {"now": True, "label": "Good time to send: a weekday morning."}
    nxt = now.replace(hour=8, minute=30, second=0, microsecond=0)
    for _ in range(8):
        if nxt > now and nxt.weekday() in good_days:
            break
        nxt += timedelta(days=1)
    return {"now": False, "next": nxt.isoformat(timespec="minutes"),
            "label": f"Best sent {nxt.strftime('%A')} morning, 8-10 am in their time zone."}


def next_step(c: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What's next for one contact: draft, send, follow up (and when), or done."""
    now = now or datetime.now()
    if c.get("replied_at"):
        return {"step": "replied", "label": "Replied"}
    if not c.get("drafts"):
        return {"step": "draft", "label": "Write the drafts"}
    if not c.get("sent_at"):
        return {"step": "send", "label": "Send the first message", "window": send_window(now)}
    n = len(c.get("followups_sent") or [])
    if n >= len(FOLLOW_UP_DAYS):
        return {"step": "done", "label": "Sequence finished"}
    due = datetime.fromisoformat(c["sent_at"]) + timedelta(days=FOLLOW_UP_DAYS[n])
    return {"step": f"followup{n + 1}", "label": f"Follow-up {n + 1}", "due": due.isoformat(timespec="minutes"),
            "overdue": now >= due}


def due_followups(private_dir: Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    out = []
    for rec in load(private_dir).values():
        for c in rec["contacts"]:
            s = next_step(c, now)
            if s.get("overdue"):
                out.append({"job_id": rec["job_id"], "company": rec["company"], "title": rec["title"],
                            "contact": c["name"], "contact_id": c["id"], **s})
    return out


def stats(private_dir: Path | None = None, days: int = 7, now: datetime | None = None) -> dict[str, int]:
    """Messages sent and replies received in the last `days`."""
    now = now or datetime.now()
    since = (now - timedelta(days=days)).isoformat()
    sent = replied = 0
    for rec in load(private_dir).values():
        for c in rec["contacts"]:
            if (c.get("sent_at") or "") >= since:
                sent += 1
            if (c.get("replied_at") or "") >= since:
                replied += 1
    return {"sent": sent, "replied": replied}


def schools(private_dir: Path | None = None) -> list[str]:
    """The schools on your profile: alumni there are the warmest referral path."""
    from pipeline import applicant
    from pipeline.resume.writer import load_profile
    names: list[str] = []
    try:
        names.append(applicant.load(private_dir).get("school", "").strip())
    except Exception:
        pass
    prof = ""
    try:
        prof = load_profile(private_dir)
    except Exception:
        pass
    for m in re.finditer(r"([A-Z][A-Za-z.&' -]{2,60}?(?:University|Institute of Technology|Institute|College)"
                         r"(?: of [A-Z][A-Za-z ]{2,30})?|IIT [A-Z][a-z]+|NYU|MIT|UCLA|CMU)", prof):
        names.append(m.group(1).strip(" ,"))
    out: list[str] = []
    for n in names:
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out[:4]


def overview(private_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every outreach record, newest first, with each contact's next step."""
    recs = sorted(load(private_dir).values(), key=lambda r: r.get("created_at", ""), reverse=True)
    for r in recs:
        for c in r["contacts"]:
            c["next"] = next_step(c)
            if c.get("drafts") and c.get("email"):
                c["compose"] = {w: compose_links(r, c, w) for w in ("email", "followup1", "followup2")}
    return recs
