"""Discover company slugs for ATS platforms.

Provides utilities to find which companies use which ATS,
so you don't have to manually maintain slug lists.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def probe_greenhouse(slug: str) -> bool:
    """Check if a company has a Greenhouse job board."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                params={"content": "false"},
            )
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def probe_ashby(slug: str) -> bool:
    """Check if a company has an Ashby job board."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def probe_lever(slug: str) -> bool:
    """Check if a company has a Lever postings page."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.lever.co/v0/postings/{slug}",
                params={"mode": "json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return isinstance(data, list)
            return False
    except httpx.HTTPError:
        return False


async def detect_ats(company_slug: str) -> str | None:
    """Try to detect which ATS a company uses by probing each platform.

    Returns the platform name or None.
    """
    slug = company_slug.lower().replace(" ", "-").replace(".", "")

    if await probe_greenhouse(slug):
        return "greenhouse"
    if await probe_ashby(slug):
        return "ashby"
    if await probe_lever(slug):
        return "lever"
    return None
