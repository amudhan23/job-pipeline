"""Search named companies for roles you name.

You list companies ("Stripe, Jane Street, Datadog") and roles ("software
engineer, backend, platform"). For each company this finds its job portal —
from boards already known, by trying the URL patterns of each ATS
(Greenhouse, Ashby, Lever, SmartRecruiters, Workable), and finally a web
search — then reads it with your roles instead of the profile's.

Saved searches (data/private/searches.json) become a watchlist: their
boards are pinned in the crawler and read with the search's roles, so a new
matching role at those companies shows up within minutes.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.crawler.fetcher import Fetcher
from pipeline.scrapers.ashby import ASHBY_API
from pipeline.scrapers.greenhouse import GREENHOUSE_API
from pipeline.scrapers.lever import LEVER_API
from pipeline.scrapers.smartrecruiters import SMARTRECRUITERS_API
from pipeline.scrapers.workable import WORKABLE_API

SEARCHES_FILE = "searches.json"
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "technologies", "technology",
             "the", "group", "holdings", "plc", "gmbh"}


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def parse_list(text: str | list) -> list[str]:
    items = text if isinstance(text, list) else re.split(r"[,\n;]+", text or "")
    seen, out = set(), []
    for it in items:
        it = " ".join(str(it).split())
        if it and it.lower() not in seen:
            seen.add(it.lower())
            out.append(it)
    return out


def slug_candidates(name: str) -> list[str]:
    """URL slugs a company is likely to use: "Jane Street" → janestreet, jane-street…"""
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    core = [w for w in words if w not in _SUFFIXES] or words
    cands = ["".join(core), "-".join(core), "".join(words), "-".join(words),
             "".join(core) + "hq", "".join(core) + "inc", "".join(core) + "labs"]
    if len(core) > 1:
        cands.append(core[0])
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _title_case(slug: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[-_]", slug))


async def _probe(fetcher: Fetcher, platform: str, slug: str) -> bool:
    if platform == "greenhouse":
        r = await fetcher.get(f"{GREENHOUSE_API}/{slug}/jobs")
        return r.ok and "jobs" in (r.json() if r.body else {})
    if platform == "ashby":
        r = await fetcher.get(f"{ASHBY_API}/{slug}")
        return r.ok and isinstance(r.json(), dict) and "jobs" in r.json()
    if platform == "lever":
        r = await fetcher.get(f"{LEVER_API}/{slug}", params={"mode": "json", "limit": 1})
        return r.ok and isinstance(r.json(), list)
    if platform == "smartrecruiters":
        r = await fetcher.get(f"{SMARTRECRUITERS_API}/{slug}/postings", params={"limit": 1})
        return r.ok and (r.json() or {}).get("totalFound", 0) > 0
    if platform == "workable":
        r = await fetcher.get(f"{WORKABLE_API}/{slug}")
        return r.ok and bool((r.json() or {}).get("name"))
    return False


def _resembles(name: str, slug: str) -> bool:
    core = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _SUFFIXES]
    s = re.sub(r"[^a-z0-9]", "", slug.lower())
    return bool(core) and (core[0] in s or "".join(core) in s)


async def resolve_company(name: str, fetcher: Fetcher, registry, web: bool = True) -> list[tuple[str, str]]:
    """Every job board we can find for one company."""
    cands = slug_candidates(name)
    # 1. Already known (by display name or slug)
    known = []
    names = registry.names()
    for b in registry.active():
        label = (names.get((b.platform, b.slug)) or "").lower()
        if label == name.lower() or b.slug.lower() in cands:
            known.append((b.platform, b.slug))
    if known:
        return known

    # 2. Try each ATS's URL pattern
    tries = [(p, c) for c in cands for p in ("greenhouse", "ashby", "lever", "workable")]
    tries += [("smartrecruiters", _title_case(c)) for c in cands[:3]]

    async def check(p: str, s: str):
        try:
            return (p, s) if await _probe(fetcher, p, s) else None
        except Exception:
            return None

    hits = [h for h in await asyncio.gather(*(check(p, s) for p, s in tries)) if h]
    found: list[tuple[str, str]] = []
    for h in hits:                                  # one board per platform is plenty
        if h[0] not in {f[0] for f in found}:
            found.append(h)
    if found or not web:
        return found

    # 3. Web search for its postings on any ATS (also finds Workday tenants)
    from pipeline.scrapers.search_discover import (_extract_slug, _get_search_backend,
                                                   _search_duckduckgo, _search_serpapi, _search_serper)
    backend, key = _get_search_backend()
    search = {"serper": _search_serper, "serpapi": _search_serpapi, "duckduckgo": _search_duckduckgo}[backend]
    query = (f'"{name}" jobs (site:boards.greenhouse.io OR site:job-boards.greenhouse.io OR '
             f'site:jobs.lever.co OR site:jobs.ashbyhq.com OR site:myworkdayjobs.com OR '
             f'site:jobs.smartrecruiters.com OR site:apply.workable.com)')
    try:
        urls = await search(query, key, None)
    except Exception:
        return []
    for u in urls:
        got = _extract_slug(u)
        if got and _resembles(name, got[1]) and got not in found:
            found.append(got)
    return found


def search_filters(config: dict[str, Any], roles: list[str], anywhere: bool) -> dict[str, Any]:
    """The profile's filters with this search's roles (and optionally no location limit)."""
    f = copy.deepcopy(config.get("filters", {}))
    if roles:
        f["role_keywords"] = [r.lower() for r in roles]
    if anywhere:
        f["locations"] = []
    return f


def scrapers_for(config: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    from pipeline.orchestrator import Pipeline
    cfg = {**config, "filters": filters}
    return Pipeline.make_scrapers(cfg)


# ── Saved searches ─────────────────────────────────────────────────────

def load_searches(private_dir: Path | None = None) -> list[dict]:
    p = _pdir(private_dir) / SEARCHES_FILE
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return []


def save_searches(items: list[dict], private_dir: Path | None = None) -> None:
    p = _pdir(private_dir) / SEARCHES_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2))
    os.chmod(p, 0o600)


def upsert_search(companies: list[str], roles: list[str], anywhere: bool = False, watch: bool = True,
                  search_id: str | None = None, private_dir: Path | None = None) -> dict:
    items = load_searches(private_dir)
    found = next((s for s in items if s["id"] == search_id), None) if search_id else None
    if found is None:
        found = {"id": uuid.uuid4().hex[:10], "created": datetime.now().isoformat(timespec="seconds")}
        items.insert(0, found)
    found.update(companies=parse_list(companies)[:50], roles=parse_list(roles)[:20],
                 anywhere=bool(anywhere), watch=bool(watch))
    save_searches(items, private_dir)
    return found


def delete_search(search_id: str, private_dir: Path | None = None) -> bool:
    items = load_searches(private_dir)
    kept = [s for s in items if s["id"] != search_id]
    save_searches(kept, private_dir)
    return len(kept) != len(items)


def watch_overrides(config: dict[str, Any], private_dir: Path | None = None) -> dict[tuple[str, str], dict]:
    """Boards from watched searches → the filters to read them with.
    A board in several searches gets all their roles."""
    out: dict[tuple[str, str], dict] = {}
    for s in load_searches(private_dir):
        if not s.get("watch"):
            continue
        for r in (s.get("results") or {}).values():
            for p, slug in r.get("boards", []):
                key = (p, slug)
                f = search_filters(config, s.get("roles", []), s.get("anywhere", False))
                # Widen, never narrow: the profile's roles stay in, so the
                # crawler doesn't "close" postings it found with them
                f["role_keywords"] = sorted(set(f.get("role_keywords", [])) |
                                            {r.lower() for r in config.get("filters", {}).get("role_keywords", [])})
                if key in out:
                    roles = set(out[key].get("role_keywords", [])) | set(f.get("role_keywords", []))
                    out[key]["role_keywords"] = sorted(roles)
                    if not f.get("locations"):
                        out[key]["locations"] = []
                else:
                    out[key] = f
    return out


async def run_search(search: dict, config: dict[str, Any], pipeline=None, fetcher: Fetcher | None = None,
                     private_dir: Path | None = None, web: bool = True,
                     progress=None) -> dict:
    """Resolve every company, read its boards with the search's roles, save
    matching jobs (scored) to the tracker. Returns the updated search."""
    from pipeline.crawler.sources import build_sources
    from pipeline.orchestrator import Pipeline
    pipeline = pipeline or Pipeline(config)
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    filters = search_filters(config, search.get("roles", []), search.get("anywhere", False))
    sources = build_sources(scrapers_for(config, filters))
    results: dict[str, dict] = {}
    try:
        for i, company in enumerate(search.get("companies", [])):
            if progress:
                progress(f"Finding {company}'s job portal ({i + 1}/{len(search['companies'])})")
            entry: dict[str, Any] = {"boards": [], "jobs": [], "error": ""}
            try:
                boards = await resolve_company(company, fetcher, pipeline.registry, web=web)
            except Exception as e:
                boards, entry["error"] = [], f"{type(e).__name__}: {e}"[:160]
            entry["boards"] = [list(b) for b in boards]
            if not boards and not entry["error"]:
                entry["error"] = "couldn't find a public job board (it may use Workday login, iCIMS or its own site)"
            for platform, slug in boards:
                pipeline.registry.add(platform, slug, "search")
                pipeline.registry.set_names({(platform, slug): company})
                src = sources.get(platform)
                if not src:
                    continue
                known = pipeline.tracker.known_ids(platform, slug)
                listing = await src.read(fetcher, slug, {}, known)
                if listing.error:
                    entry["error"] = f"{platform}: {listing.error}"
                    continue
                jobs = listing.jobs
                for j in jobs:
                    j.company = company
                fresh = [j for j in jobs if j.id not in known]
                if fresh:
                    pipeline.sponsorship.enrich_jobs(fresh)
                    pipeline.scorer.score_jobs(fresh)
                # Postings that were already up aren't "just opened"; nothing is closed here
                pipeline.tracker.observe_board(platform, slug, jobs, baseline=True, close_missing=False)
                entry["jobs"] += [j.id for j in jobs]
            results[company] = entry
    finally:
        if own_fetcher:
            await fetcher.aclose()
    if search.get("watch"):
        pipeline.registry.make_due_now({tuple(b) for r in results.values() for b in r["boards"]})
    search["results"] = results
    search["last_run"] = datetime.now().isoformat(timespec="seconds")
    items = load_searches(private_dir)
    for k, s in enumerate(items):
        if s["id"] == search.get("id"):
            items[k] = search
    save_searches(items, private_dir)
    return search
