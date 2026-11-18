"""Seed the board list with every company that has hired new grads or interns.

SimplifyJobs keeps every listing it has ever published (open or closed) in
its listings.json files. Each job link names the company's ATS board, so the
full history is a list of ~4,000 companies on Greenhouse, Lever, Ashby,
Workday and others that hire early-career engineers. Watching all of those
boards catches their next opening the moment it goes live, instead of only
the companies with a role open in the feed today.

Refreshed once a week; new companies are added, nothing is removed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SEED_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/.github/scripts/listings.json",
]
EVERY = timedelta(days=7)
STATE = Path("data/seed_state.json")


def harvest(listings: list[dict[str, Any]]) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """Every scrapable board any listing points at, open or closed, any age."""
    from pipeline.discovery.feeds import _platform_for
    boards: set[tuple[str, str]] = set()
    names: dict[tuple[str, str], str] = {}
    for item in listings:
        url = item.get("url") or ""
        if not url:
            continue
        platform, slug = _platform_for(url)
        if not slug:
            continue
        key = (platform.value, slug)
        boards.add(key)
        if item.get("company_name"):
            names[key] = item["company_name"]
    return boards, names


def state(path: Path = STATE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def due(path: Path = STATE, now: datetime | None = None) -> bool:
    at = state(path).get("at")
    if not at:
        return True
    try:
        return (now or datetime.now()) - datetime.fromisoformat(at) >= EVERY
    except ValueError:
        return True


def save(info: dict[str, Any], path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **info}, indent=1))


def run_sync(registry, crawlable: set[str] | None = None, path: Path = STATE) -> dict[str, Any]:
    """Download the lists and add their boards (for the CLI)."""
    import httpx
    boards: set[tuple[str, str]] = set()
    names: dict[tuple[str, str], str] = {}
    errors = []
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for url in SEED_URLS:
            try:
                r = client.get(url)
                r.raise_for_status()
                b, n = harvest(r.json())
                boards |= b
                names.update(n)
            except Exception as e:      # one list failing shouldn't stop the others
                errors.append(f"{url.split('/')[4]}: {e}"[:160])
    if crawlable is not None:
        boards = {b for b in boards if b[0] in crawlable}
    added = registry.add_many(boards, source="seed")
    registry.set_names({k: v for k, v in names.items() if k in boards})
    info = {"boards": len(boards), "new": added, "errors": errors}
    if boards:
        save(info, path)
    return info
