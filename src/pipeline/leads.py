"""Hiring posts from people, not job boards.

Founders and engineers often post "we're hiring" before (or instead of) a
job board listing. jobpipe collects those posts from:

  - Hacker News: the monthly "Ask HN: Who is hiring?" thread and YC job posts
    (free official API);
  - Bluesky: public post search (free official API);
  - X: recent search through the official API, if you add a bearer token
    (pay-per-use, about $0.005 per post read; a daily cap keeps it cheap);
  - posts you send in: paste on the dashboard, or forward text, a link or a
    screenshot (e.g. of a LinkedIn post) to your Telegram bot.

LinkedIn itself is never scraped: its User Agreement forbids automated
collection and accounts get restricted for it. Send posts you see instead.

Each post is read by Claude (company, role, location, visa, apply link) and
scored against your profile. Good ones: the company's job board is added to
the crawler (so the real posting is caught and applied to like any other),
and you get a Telegram alert with the post and its author (often the hiring
manager, worth a direct note).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

LEADS_DB = Path(__file__).resolve().parents[2] / "data" / "leads.db"
SETTINGS = "leads.json"
HN_API = "https://hn.algolia.com/api/v1"
BSKY_API = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"   # public.api.bsky.app refuses search
X_API = "https://api.x.com/2/tweets/search/recent"
X_PRICE_PER_POST = 0.005

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "hn": True, "bluesky": True, "x": False,
    "queries": ['"we\'re hiring" engineer', '"we are hiring" software', '#hiring software engineer',
                '"hiring" backend engineer', '"join our team" engineer'],
    "x_daily_posts": 100,            # ≈ $0.50 a day at X's pay-per-use rate
    "alert_min_score": 70,
    "auto_watch": True,              # add the company's job board to the crawler for good leads
    "every_minutes": 20,
}

_HIRING = re.compile(r"\b(hiring|we're hiring|we are hiring|join (our|my) team|open (role|position)s?|"
                     r"looking for (a|an)?\s*\w*\s*(engineer|developer)|is hiring)\b", re.I)
_SEEKING = re.compile(r"\b(i'?m looking for (a )?(job|role|work)|open to work|seeking (a )?(new )?(role|position)|"
                      r"#opentowork|hire me)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Settings (X token lives here, so it's private) ─────────────────────

def load_settings(private_dir: Path | None = None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads((_pdir(private_dir) / SETTINGS).read_text()))
    except (OSError, ValueError):
        pass
    if os.environ.get("JOBPIPE_X_BEARER"):
        s["x_token"] = os.environ["JOBPIPE_X_BEARER"]
    return s


def save_settings(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    s = load_settings(private_dir)
    for k, v in values.items():
        if k == "x_token":
            v = str(v).strip()
            if v and not re.fullmatch(r"[A-Za-z0-9%_\-.=]{40,300}", v):
                raise ValueError("That doesn't look like an X API bearer token.")
            if v or values.get("clear_x_token"):
                s["x_token"] = v
        elif k == "bsky_handle":
            s[k] = str(v).strip().lstrip("@")[:100]
        elif k == "bsky_app_password":
            if v:
                s[k] = str(v).strip()[:64]
        elif k == "queries":
            items = v if isinstance(v, list) else str(v).splitlines()
            s["queries"] = [q.strip()[:120] for q in items if q.strip()][:12]
        elif k in DEFAULTS and isinstance(DEFAULTS[k], bool):
            s[k] = bool(v)
        elif k in DEFAULTS and isinstance(DEFAULTS[k], int):
            lo, hi = {"x_daily_posts": (0, 5000), "alert_min_score": (0, 100), "every_minutes": (5, 1440)}[k]
            s[k] = max(lo, min(hi, int(v)))
    p = _pdir(private_dir) / SETTINGS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2))
    os.chmod(p, 0o600)
    return public_settings(private_dir)


def public_settings(private_dir: Path | None = None) -> dict[str, Any]:
    s = load_settings(private_dir)
    out = {k: s[k] for k in DEFAULTS}
    out["has_x_token"] = bool(s.get("x_token"))
    out["bsky_handle"] = s.get("bsky_handle", "")
    out["has_bsky_password"] = bool(s.get("bsky_app_password"))
    out["x_today"] = LeadStore().x_reads_today()
    return out


# ── Storage ────────────────────────────────────────────────────────────

class LeadStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else LEADS_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, source TEXT, url TEXT, author TEXT, author_url TEXT,
                    text TEXT, posted_at TEXT, found_at TEXT, hiring INTEGER);
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY, post_id TEXT, source TEXT, url TEXT, author TEXT, author_url TEXT,
                    snippet TEXT, posted_at TEXT, found_at TEXT, company TEXT, role TEXT, location TEXT,
                    remote INTEGER, visa TEXT, apply_url TEXT, contact TEXT, score REAL, reason TEXT,
                    status TEXT DEFAULT 'new', board TEXT, alerted INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS leads_status ON leads(status, score);
                CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def get(self, k: str, default: str = "") -> str:
        with self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def put(self, k: str, v: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def seen(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self._conn() as c:
            q = ",".join("?" * len(ids))
            return {r["id"] for r in c.execute(f"SELECT id FROM posts WHERE id IN ({q})", ids)}

    def add_post(self, p: dict, hiring: bool) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?,?)",
                      (p["id"], p["source"], p.get("url", ""), p.get("author", ""), p.get("author_url", ""),
                       p.get("text", "")[:6000], p.get("posted_at", ""), utcnow().isoformat(), int(hiring)))

    def add_lead(self, lead: dict) -> bool:
        cols = ["id", "post_id", "source", "url", "author", "author_url", "snippet", "posted_at", "found_at",
                "company", "role", "location", "remote", "visa", "apply_url", "contact", "score", "reason"]
        with self._conn() as c:
            cur = c.execute(f"INSERT OR IGNORE INTO leads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [lead.get(k) for k in cols])
            return cur.rowcount > 0

    def lead(self, lead_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(r) if r else None

    def update(self, lead_id: str, **values: Any) -> None:
        allowed = {"status", "board", "alerted", "score", "reason"}
        sets = {k: v for k, v in values.items() if k in allowed}
        if sets:
            with self._conn() as c:
                c.execute(f"UPDATE leads SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), lead_id])

    def list(self, status: str | None = None, limit: int = 200, min_score: float = 0) -> list[dict]:
        q, args = "SELECT * FROM leads WHERE score >= ?", [min_score]
        if status:
            q += " AND status = ?"
            args.append(status)
        q += " ORDER BY COALESCE(posted_at, found_at) DESC LIMIT ?"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, [*args, limit])]

    def counts(self) -> dict[str, Any]:
        with self._conn() as c:
            by = {r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM leads GROUP BY source")}
            posts = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        return {"by_source": by, "posts_read": posts}

    def x_reads_today(self) -> int:
        day = utcnow().strftime("%Y-%m-%d")
        return int(self.get(f"x_reads:{day}", "0") or 0)

    def add_x_reads(self, n: int) -> None:
        day = utcnow().strftime("%Y-%m-%d")
        self.put(f"x_reads:{day}", str(self.x_reads_today() + n))


# ── Sources: each returns posts {id, source, url, author, author_url, text, posted_at, links} ──

def _strip_html(s: str) -> str:
    s = re.sub(r"<p>", "\n", s or "")
    s = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>.*?</a>", r" \1 ", s)
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def hn_posts(client: httpx.Client, store: LeadStore, max_items: int = 300) -> list[dict]:
    """New top-level comments on the latest "Who is hiring?" thread, plus YC job posts."""
    out: list[dict] = []
    r = client.get(f"{HN_API}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 6})
    r.raise_for_status()
    thread = next((h for h in r.json().get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if thread:
        tid = str(thread["objectID"])
        since = int(store.get(f"hn_since:{tid}", "0") or 0)
        page = 0
        newest = since
        while len(out) < max_items and page < 10:
            r = client.get(f"{HN_API}/search_by_date", params={
                "tags": f"comment,story_{tid}", "hitsPerPage": 100, "page": page,
                "numericFilters": f"created_at_i>{since}"})
            r.raise_for_status()
            hits = r.json().get("hits", [])
            if not hits:
                break
            for h in hits:
                newest = max(newest, int(h.get("created_at_i") or 0))
                if str(h.get("parent_id")) != tid or not h.get("comment_text"):
                    continue                                   # replies, not postings
                out.append({"id": f"hn:{h['objectID']}", "source": "hn",
                            "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                            "author": h.get("author", ""), "author_url": f"https://news.ycombinator.com/user?id={h.get('author', '')}",
                            "text": _strip_html(h["comment_text"]), "posted_at": h.get("created_at", ""),
                            "thread": thread.get("title", "")})
            page += 1
        store.put(f"hn_since:{tid}", str(newest))
    r = client.get(f"{HN_API}/search_by_date", params={"tags": "job", "hitsPerPage": 40,
                                                        "numericFilters": f"created_at_i>{int(time.time()) - 7 * 86400}"})
    if r.status_code == 200:
        for h in r.json().get("hits", []):
            out.append({"id": f"hnjob:{h['objectID']}", "source": "hn", "url": h.get("url") or
                        f"https://news.ycombinator.com/item?id={h['objectID']}",
                        "author": h.get("author", ""), "author_url": "", "posted_at": h.get("created_at", ""),
                        "text": (h.get("title") or "") + "\n" + _strip_html(h.get("story_text") or "") +
                                (("\n" + h["url"]) if h.get("url") else "")})
    return out


def _bsky_links(post: dict) -> list[str]:
    links = []
    rec = post.get("record") or {}
    for f in rec.get("facets") or []:
        for feat in f.get("features") or []:
            if feat.get("uri"):
                links.append(feat["uri"])
    ext = ((post.get("embed") or {}).get("external") or {}).get("uri")
    if ext:
        links.append(ext)
    return links


def _bsky_session(client: httpx.Client, handle: str, app_password: str) -> str | None:
    """Sign in with a Bluesky app password (free) when public search is refused."""
    r = client.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                    json={"identifier": handle, "password": app_password})
    return r.json().get("accessJwt") if r.status_code == 200 else None


def bluesky_posts(client: httpx.Client, store: LeadStore, queries: list[str],
                  handle: str = "", app_password: str = "") -> list[dict]:
    out: list[dict] = []
    since = store.get("bsky_since") or (utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    url, headers, statuses = BSKY_API, {}, []
    for q in queries:
        params = {"q": q, "sort": "latest", "limit": 50, "since": since, "lang": "en"}
        r = client.get(url, params=params, headers=headers)
        if r.status_code in (401, 403) and handle and app_password and not headers:
            token = _bsky_session(client, handle, app_password)
            if token:
                url = "https://bsky.social/xrpc/app.bsky.feed.searchPosts"
                headers = {"Authorization": f"Bearer {token}"}
                r = client.get(url, params=params, headers=headers)
        statuses.append(r.status_code)
        if r.status_code != 200:
            continue
        for p in r.json().get("posts", []):
            handle = (p.get("author") or {}).get("handle", "")
            rkey = (p.get("uri") or "").rsplit("/", 1)[-1]
            text = (p.get("record") or {}).get("text", "")
            links = _bsky_links(p)
            out.append({"id": f"bsky:{p.get('uri')}", "source": "bluesky",
                        "url": f"https://bsky.app/profile/{handle}/post/{rkey}", "author": handle,
                        "author_url": f"https://bsky.app/profile/{handle}",
                        "text": text + ("\n" + "\n".join(links) if links else ""),
                        "posted_at": (p.get("record") or {}).get("createdAt", "")})
    if statuses and all(c != 200 for c in statuses):
        hint = " (add a Bluesky handle and app password on the Leads page)" if 403 in statuses or 401 in statuses else ""
        raise RuntimeError(f"Bluesky search answered {statuses[0]}{hint}")
    store.put("bsky_since", started)
    return out


def x_posts(client: httpx.Client, store: LeadStore, token: str, queries: list[str], daily_cap: int) -> list[dict]:
    """Recent X posts through the official API. Stops at your daily cap."""
    left = daily_cap - store.x_reads_today()
    if left <= 0 or not token:
        return []
    terms = " OR ".join(f"({q})" for q in queries[:5])
    query = f"({terms}) -is:retweet -is:reply lang:en"[:512]
    params: dict[str, Any] = {"query": query, "max_results": max(10, min(100, left)),
                              "tweet.fields": "created_at,entities,author_id"}
    since_id = store.get("x_since_id")
    if since_id:
        params["since_id"] = since_id
    r = client.get(X_API, params=params, headers={"Authorization": f"Bearer {token}"})
    if r.status_code in (401, 403):
        raise PermissionError("X rejected the bearer token (or the account has no credits).")
    if r.status_code == 429:
        return []
    r.raise_for_status()
    data = r.json()
    tweets = data.get("data") or []
    store.add_x_reads(len(tweets))
    if (data.get("meta") or {}).get("newest_id"):
        store.put("x_since_id", data["meta"]["newest_id"])
    out = []
    for t in tweets:
        urls = [u.get("expanded_url") for u in ((t.get("entities") or {}).get("urls") or []) if u.get("expanded_url")]
        out.append({"id": f"x:{t['id']}", "source": "x", "url": f"https://x.com/i/web/status/{t['id']}",
                    "author": t.get("author_id", ""), "author_url": f"https://x.com/i/user/{t.get('author_id', '')}",
                    "text": t.get("text", "") + ("\n" + "\n".join(urls) if urls else ""),
                    "posted_at": t.get("created_at", "")})
    return out


# ── Reading the posts ──────────────────────────────────────────────────

_EXTRACT = """You read social posts and decide which are someone HIRING for a role.

Not hiring: people looking for work, recruiters' generic spam with no company, courses,
ads, crypto/token promotions, jokes, news about layoffs.

For each hiring post, list its openings (several if it names several roles or
companies). Use only what the post says; leave a field empty if it doesn't say.
visa: "yes" if it says it sponsors visas / welcomes visa holders, "no" if it says
no sponsorship / must be authorized already, else "unknown".
remote: true, false, or null if not said.
apply_url: a link from the post to apply or to the job (prefer job boards like
greenhouse, lever, ashby, workday), else "".

Reply with JSON only:
{"results": [{"i": 0, "hiring": true, "openings": [{"company": "", "role": "", "location": "",
  "remote": null, "visa": "unknown", "apply_url": "", "contact": ""}]}]}"""


def _quick_filter(p: dict) -> bool:
    t = p.get("text", "")
    if p["source"] in ("hn", "manual", "linkedin_email"):
        return len(t) > 20
    return bool(_HIRING.search(t)) and not _SEEKING.search(t)


def extract_rules(p: dict) -> list[dict]:
    """No-AI reading. HN postings follow "Company | Role | Location | ..."."""
    text = p.get("text", "")
    first = text.strip().splitlines()[0] if text.strip() else ""
    links = _URL.findall(text)
    ats = next((u for u in links if re.search(r"greenhouse|lever\.co|ashbyhq|myworkdayjobs|workable|smartrecruiters", u)), "")
    no = re.search(r"\b(no|not|unable to|cannot|can't|don't|do not) (offer |provide |sponsor )?(visa )?sponsor|"
                   r"must be (authorized|eligible) to work|us citizens? only|clearance required|without (visa )?sponsorship",
                   text, re.I)
    yes = re.search(r"\bvisa (sponsor|support|transfer)|sponsor(ship)? (is )?(available|provided|offered)|"
                    r"h-?1b (ok|welcome|sponsor|transfer)|will sponsor|we sponsor", text, re.I)
    visa = "no" if no else "yes" if yes else "unknown"
    remote = True if re.search(r"\bremote\b", text, re.I) else None
    if p["source"] == "hn" and "|" in first:
        parts = [x.strip() for x in first.split("|")]
        company = re.sub(r"\s*\(.*?\)\s*$", "", parts[0])[:80]
        role = next((x for x in parts[1:] if re.search(r"engineer|developer|scientist|sre|devops|backend|frontend|full.?stack", x, re.I)), parts[1] if len(parts) > 1 else "")
        loc = next((x for x in parts[1:] if re.search(r"remote|onsite|hybrid|ny|new york|sf|san francisco|london|usa|us\b", x, re.I)), "")
        return [{"company": company, "role": role[:120], "location": loc[:80], "remote": remote, "visa": visa,
                 "apply_url": ats or (links[0] if links else ""), "contact": ""}]
    m = re.search(r"(?:hiring|looking for)\s+(?:an?\s+)?((?:senior|staff|lead|principal|junior|founding)?\s*[\w+#./-]*\s*(?:software|backend|frontend|full[- ]?stack|platform|infrastructure|ml|data)?\s*(?:engineer|developer)s?)", text, re.I)
    if not m:
        return []
    return [{"company": "", "role": m.group(1).strip()[:120], "location": "", "remote": remote, "visa": visa,
             "apply_url": ats or (links[0] if links else ""), "contact": ""}]


def extract_ai(posts: list[dict], private_dir: Path | None = None, client: Any = None) -> dict[int, list[dict]]:
    from pipeline.ai import client as ai
    body = "\n\n".join(f"<post i=\"{i}\" source=\"{p['source']}\">\n{p['text'][:2500]}\n</post>"
                       for i, p in enumerate(posts))
    reply = ai.complete(_EXTRACT, body, purpose=f"read {len(posts)} hiring posts", max_tokens=2500,
                        private_dir=private_dir, client=client, model="haiku")
    m = re.search(r"\{.*\}", reply.text, re.S)
    data = json.loads(m.group(0)) if m else {}
    out: dict[int, list[dict]] = {}
    for r in data.get("results") or []:
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        if r.get("hiring") and 0 <= i < len(posts):
            out[i] = [o for o in r.get("openings") or [] if isinstance(o, dict)]
    return out


def score_opening(o: dict, p: dict, config: dict) -> tuple[float, str]:
    """0–100 against your profile: role match, location, visa, freshness."""
    from pipeline.models import ATSPlatform, Job
    from pipeline.scoring.scorer import FitScorer
    filters = config.get("filters", {})
    role_kw = [k.lower() for k in filters.get("role_keywords", [])]
    role = (o.get("role") or "").lower()
    title_hit = any(k in role for k in role_kw) if role_kw else bool(re.search(r"engineer|developer", role))
    excluded = any(x.lower() in role for x in filters.get("exclude_keywords", []))
    try:
        job = Job(id="lead", title=o.get("role") or "", company=o.get("company") or "", company_slug="",
                  location=o.get("location") or ("Remote" if o.get("remote") else ""), url=p.get("url", ""),
                  platform=ATSPlatform.GREENHOUSE, description=p.get("text", "")[:4000])
        base = FitScorer(config).score(job)
    except Exception:
        base = 50.0
    score = base if title_hit else base * 0.5
    if excluded:
        score *= 0.3
    reasons = []
    if title_hit:
        reasons.append("role matches")
    if o.get("visa") == "yes":
        score += 15
        reasons.append("mentions visa sponsorship")
    elif o.get("visa") == "no":
        score -= 40
        reasons.append("says no sponsorship")
    if not o.get("company"):
        score -= 10
    return max(0.0, min(100.0, round(score, 1))), ", ".join(reasons)


def process(posts: list[dict], store: LeadStore, config: dict, private_dir: Path | None = None,
            ai_client: Any = None) -> list[dict]:
    """Read new posts, store hiring ones as leads. Returns the new leads."""
    from pipeline.ai import client as ai
    fresh = [p for p in posts if p["id"] not in store.seen([p["id"] for p in posts])]
    candidates = [p for p in fresh if _quick_filter(p)]
    for p in fresh:
        if p not in candidates:
            store.add_post(p, hiring=False)
    use_ai = ai.status(private_dir)[0]
    found: dict[str, list[dict]] = {}
    for i in range(0, len(candidates), 12):
        batch = candidates[i:i + 12]
        got: dict[int, list[dict]] = {}
        if use_ai:
            try:
                got = extract_ai(batch, private_dir, ai_client)
            except Exception:
                got = {}
                use_ai = False                         # budget or key problem: rules for the rest
        if not got and not use_ai:
            got = {j: o for j, p in enumerate(batch) if (o := extract_rules(p))}
        for j, p in enumerate(batch):
            store.add_post(p, hiring=j in got)
            if j in got:
                found[p["id"]] = got[j]
    new = []
    by_id = {p["id"]: p for p in candidates}
    for pid, openings in found.items():
        p = by_id[pid]
        for n, o in enumerate(openings[:6]):
            score, reason = score_opening(o, p, config)
            lead = {"id": f"{pid}#{n}", "post_id": pid, "source": p["source"], "url": p.get("url", ""),
                    "author": p.get("author", ""), "author_url": p.get("author_url", ""),
                    "snippet": p.get("text", "")[:600], "posted_at": p.get("posted_at", ""),
                    "found_at": utcnow().isoformat(), "company": (o.get("company") or "")[:100],
                    "role": (o.get("role") or "")[:160], "location": (o.get("location") or "")[:100],
                    "remote": None if o.get("remote") is None else int(bool(o.get("remote"))),
                    "visa": o.get("visa") if o.get("visa") in ("yes", "no") else "unknown",
                    "apply_url": (o.get("apply_url") or "")[:500], "contact": (o.get("contact") or "")[:200],
                    "score": score, "reason": reason}
            if store.add_lead(lead):
                new.append(lead)
    return new


# ── Acting on a lead ───────────────────────────────────────────────────

def board_for(lead: dict) -> tuple[str, str] | None:
    """The job board behind a lead's link, if it points at one."""
    from pipeline.scrapers.search_discover import _extract_slug
    for u in [lead.get("apply_url", "")] + _URL.findall(lead.get("snippet", "")):
        if u and (got := _extract_slug(u)):
            return got
    return None


def watch(lead: dict, registry: Any = None, resolve: Callable[[str], list[tuple[str, str]]] | None = None) -> str:
    """Add the lead's company job board to the crawler. Returns what happened."""
    if registry is None:
        from pipeline.discovery.registry import BoardRegistry
        registry = BoardRegistry()
    boards = [b for b in [board_for(lead)] if b]
    if not boards and lead.get("company") and resolve:
        boards = resolve(lead["company"])
    if not boards:
        return ""
    for platform, slug in boards:
        registry.add(platform, slug, "lead")
    return ", ".join(f"{p}/{s}" for p, s in boards)


def _resolver() -> Callable[[str], list[tuple[str, str]]]:
    def resolve(name: str) -> list[tuple[str, str]]:
        import asyncio
        from pipeline.crawler.fetcher import Fetcher
        from pipeline.discovery.registry import BoardRegistry
        from pipeline.search import resolve_company

        async def go():
            f = Fetcher()
            try:
                return await resolve_company(name, f, BoardRegistry(), web=True)
            finally:
                await f.aclose()
        try:
            return asyncio.run(go())
        except Exception:
            return []
    return resolve


def add_shared(text: str, store: LeadStore | None = None, config: dict | None = None,
               private_dir: Path | None = None, image: bytes | None = None, ai_client: Any = None) -> list[dict]:
    """A post you sent in (pasted text, a link, or a screenshot)."""
    from pipeline.ai import client as ai
    store = store or LeadStore()
    config = config if config is not None else _config()
    text = (text or "").strip()
    if image:
        if not ai.status(private_dir)[0]:
            raise ValueError("Reading screenshots needs your Anthropic key.")
        import base64
        reply = ai.complete("Transcribe the social media post in this screenshot: author name and headline, "
                            "then the post text, then any links. Plain text only.",
                            [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                           "data": base64.b64encode(image).decode()}},
                             {"type": "text", "text": "Transcribe this post."}],
                            purpose="read a shared screenshot", max_tokens=1200, private_dir=private_dir,
                            client=ai_client, model="haiku")
        text = (reply.text.strip() + ("\n" + text if text else "")).strip()
    if len(_URL.sub("", text).strip()) < 15:
        raise ValueError("Send the post's text or a screenshot along with the link: jobpipe doesn't open "
                         "LinkedIn or X pages itself.")
    links = _URL.findall(text)
    pid = "manual:" + str(abs(hash(text)) % 10**12)
    post = {"id": pid, "source": "manual", "url": links[0] if links else "", "author": "", "author_url": "",
            "text": text[:6000], "posted_at": utcnow().isoformat()}
    return process([post], store, config, private_dir, ai_client)


def _config() -> dict:
    from pipeline.utils.config import load_config
    try:
        return load_config()
    except Exception:
        return {}


# ── Running on a schedule ──────────────────────────────────────────────

def fetch(sources: list[str] | None = None, private_dir: Path | None = None, store: LeadStore | None = None,
          client: httpx.Client | None = None, config: dict | None = None,
          ai_client: Any = None) -> dict[str, Any]:
    """One pass over the enabled sources. Returns counts and the new leads."""
    s = load_settings(private_dir)
    store = store or LeadStore()
    config = config if config is not None else _config()
    want = sources or [k for k in ("hn", "bluesky", "x") if s.get(k)]
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True,
                                    headers={"User-Agent": "jobpipe/1.0 (personal job search)"})
    report: dict[str, Any] = {"posts": {}, "errors": {}, "new": []}
    try:
        for src in want:
            try:
                if src == "hn":
                    posts = hn_posts(client, store)
                elif src == "bluesky":
                    posts = bluesky_posts(client, store, s["queries"], s.get("bsky_handle", ""),
                                          s.get("bsky_app_password", ""))
                elif src == "x":
                    posts = x_posts(client, store, s.get("x_token", ""), s["queries"], s["x_daily_posts"])
                else:
                    continue
                report["posts"][src] = len(posts)
                report["new"] += process(posts, store, config, private_dir, ai_client)
            except Exception as e:
                report["errors"][src] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if own:
            client.close()
    return report


_RUNNING = threading.Lock()


def maybe_run(tg: Any = None, private_dir: Path | None = None, store: LeadStore | None = None,
              background: bool = True, **kw: Any) -> bool:
    """Called after crawler passes: fetch every N minutes, alert on good leads,
    add their companies' boards to the crawler."""
    s = load_settings(private_dir)
    if not s["enabled"]:
        return False
    store = store or LeadStore()
    last = float(store.get("last_run", "0") or 0)
    if time.time() - last < s["every_minutes"] * 60 or not _RUNNING.acquire(blocking=False):
        return False
    store.put("last_run", str(time.time()))

    def job() -> None:
        try:
            rep = fetch(private_dir=private_dir, store=store, **kw)
            good = [x for x in rep["new"] if x["score"] >= s["alert_min_score"]]
            for lead in good:
                if s["auto_watch"]:
                    try:
                        b = watch(lead, resolve=_resolver())
                        if b:
                            store.update(lead["id"], board=b)
                            lead["board"] = b
                    except Exception:
                        pass
                if tg is not None and tg.configured():
                    try:
                        tg.notify_lead(lead)
                        store.update(lead["id"], alerted=1)
                    except Exception:
                        pass
        finally:
            _RUNNING.release()

    if background:
        threading.Thread(target=job, name="leads", daemon=True).start()
    else:
        job()
    return True
