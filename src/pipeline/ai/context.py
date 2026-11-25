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


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _rendered_text(url: str) -> str:
    """The page as a browser shows it, for sites that refuse plain requests
    or draw the posting with JavaScript (Ashby, Lever's newer pages)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=_BROWSER_HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
            browser.close()
        return " ".join((text or "").split())
    except Exception:
        return ""


def _api_description(url: str, title_hint: str = "") -> tuple[str, dict[str, str]]:
    """The posting straight from the board's public API (Ashby, Lever,
    Greenhouse): clean text, no JavaScript, no bot wall. Returns the
    description and whatever else the API knows (title, location, url). An
    Ashby board URL with a title hint picks the live posting with that title."""
    import httpx
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)(?:/([0-9a-f-]{36}))?/?$", url, re.I)
    if m:
        r = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}", timeout=20)
        jobs = r.json().get("jobs", []) if r.status_code == 200 else []
        want = (m.group(2) or "").lower()
        hint = title_hint.strip().lower()
        for j in jobs:
            if (want and j.get("id", "").lower() == want) or \
                    (not want and hint and hint in (j.get("title") or "").lower()):
                return j.get("descriptionPlain") or _page_text(j.get("descriptionHtml") or ""), \
                    {"title": j.get("title", ""), "location": j.get("location", ""), "url": j.get("jobUrl", "")}
        return "", {}
    m = re.match(r"https?://jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url, re.I)
    if m:
        r = httpx.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", timeout=20)
        if r.status_code == 200:
            j = r.json()
            return j.get("descriptionPlain") or _page_text(j.get("description") or ""), \
                {"title": j.get("text", ""), "location": (j.get("categories") or {}).get("location", "")}
        return "", {}
    m = re.match(r"https?://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", url, re.I)
    if m:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", timeout=20)
        if r.status_code == 200:
            j = r.json()
            import html as _html
            return _page_text(_html.unescape(j.get("content") or "")), \
                {"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", "")}
    return "", {}


def ensure_description(job: dict[str, Any]) -> dict[str, Any]:
    """The job with its description; fetches the posting page when the feed
    that found it didn't carry one (SimplifyJobs listings have none). Plain
    request first; a rendered browser page when the site refuses it or
    draws the posting with JavaScript."""
    if len(re.sub(r"<[^>]+>", " ", job.get("description") or "").split()) >= 60 or not job.get("url"):
        return job
    text, known = "", {}
    try:
        text, known = _api_description(job["url"], job.get("title") or "")
    except Exception:
        pass
    if len(text.split()) >= 60:
        return {**job, "description": text[:15000],
                **{k: v for k, v in known.items() if v and (not job.get(k) or k in ("url", "title"))}}
    try:
        import httpx
        r = httpx.get(job["url"], timeout=20, follow_redirects=True, headers=_BROWSER_HEADERS)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
            text = _page_text(r.text)
    except Exception:
        pass
    if len(text.split()) < 60:
        text = _rendered_text(job["url"])
    if len(text.split()) >= 60:
        return {**job, "description": text[:15000]}
    return job


def job_from_url(url: str, company: str = "", title: str = "", description: str = "",
                 tracker: Any = None) -> dict[str, Any]:
    """A job record for a posting the crawler may never have seen: the saved
    job when the URL is known, otherwise built from the page (its <title>
    fills in company/title when they weren't given)."""
    url = url.strip()
    saved = tracker.get_job_by_url(url) if tracker is not None else None
    job: dict[str, Any] = dict(saved) if saved else {
        "id": "adhoc_" + re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"^https?://(www\.)?", "", url))[:60].strip("_"),
        "url": url, "company": "", "title": "", "location": "", "description": "", "source": "adhoc"}
    if company:
        job["company"] = company
    if title:
        job["title"] = title
    if description.strip():
        job["description"] = description.strip()
    job = ensure_description(job)
    if not (job.get("company") and job.get("title")):
        head = _page_title(url)
        if head:
            # Most boards title the page "Role - Company" or "Company - Role"; the
            # writer only needs both names somewhere, so a guess is good enough.
            parts = [p.strip() for p in re.split(r"\s[-|–—@]\s|\s+at\s+", head) if p.strip()]
            if len(parts) >= 2:
                job.setdefault("title", "")
                job["title"] = job["title"] or parts[0]
                job["company"] = job["company"] or parts[-1]
            else:
                job["title"] = job["title"] or head
    return job


def _page_title(url: str) -> str:
    try:
        import httpx
        r = httpx.get(url, timeout=20, follow_redirects=True, headers=_BROWSER_HEADERS)
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
        return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())[:150] if m else ""
    except Exception:
        return ""


def load_resume_text(private_dir: Path | None = None) -> str:
    from pipeline import applicant
    from pipeline.ai.client import _pdir
    path = _pdir(private_dir) / applicant.RESUME_FILE
    try:
        return applicant.resume_text(path.read_bytes()) if path.exists() else ""
    except Exception:
        return ""
