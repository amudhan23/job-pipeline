"""Polite HTTP for the crawler.

  - Per host: at most `per_host` requests in flight and a minimum gap
    between request starts. Most boards share a host (boards-api.
    greenhouse.io, api.lever.co…), so this is what keeps us a good citizen.
  - Adaptive backoff: a 429/503 (honouring Retry-After) pauses that host
    and doubles its gap; quiet success slowly restores it.
  - Conditional GETs: send If-None-Match / If-Modified-Since when we have
    validators; a 304 costs the server almost nothing.
  - Retries with exponential backoff on network errors and 5xx.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

USER_AGENT = "jobpipe/1.0 (personal job search; polite polling)"


@dataclass
class FetchResult:
    url: str
    status: int                       # 0 = network error
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self) -> Any:
        import json
        return json.loads(self.body)


@dataclass
class _Host:
    gap: float
    base_gap: float
    sem: asyncio.Semaphore
    next_start: float = 0.0
    paused_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Fetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, per_host: int = 4,
                 min_gap: float = 0.25, timeout: float = 30.0, retries: int = 2) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        self.per_host, self.min_gap, self.retries = per_host, min_gap, retries
        self.hosts: dict[str, _Host] = {}
        self.stats = {"requests": 0, "not_modified": 0, "errors": 0, "throttled": 0, "bytes": 0}

    def _host(self, url: str) -> _Host:
        name = urlsplit(url).netloc
        if name not in self.hosts:
            self.hosts[name] = _Host(gap=self.min_gap, base_gap=self.min_gap,
                                     sem=asyncio.Semaphore(self.per_host))
        return self.hosts[name]

    async def _wait_turn(self, host: _Host) -> None:
        async with host.lock:
            now = time.monotonic()
            start = max(now, host.next_start, host.paused_until)
            host.next_start = start + host.gap
        if start > now:
            await asyncio.sleep(start - now)

    def _throttle(self, host: _Host, resp: httpx.Response | None) -> float:
        """Server said slow down: pause the host and widen its gap."""
        wait = 30.0
        if resp is not None:
            ra = resp.headers.get("retry-after", "")
            if ra.isdigit():
                wait = float(ra)
            elif ra:
                try:
                    wait = max(1.0, parsedate_to_datetime(ra).timestamp() - time.time())
                except (TypeError, ValueError):
                    pass
        wait = min(wait, 600.0)
        host.paused_until = time.monotonic() + wait
        host.gap = min(host.gap * 2, 10.0)
        self.stats["throttled"] += 1
        return wait

    async def request(self, method: str, url: str, *, params: dict | None = None,
                      json: Any = None, etag: str | None = None,
                      last_modified: str | None = None) -> FetchResult:
        host = self._host(url)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        attempt = 0
        async with host.sem:
            while True:
                await self._wait_turn(host)
                t0 = time.monotonic()
                self.stats["requests"] += 1
                try:
                    resp = await self.client.request(method, url, params=params, json=json,
                                                     headers=headers)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    if attempt < self.retries:
                        attempt += 1
                        await asyncio.sleep((2 ** attempt) + random.random())
                        continue
                    self.stats["errors"] += 1
                    return FetchResult(url, 0, error=f"{type(e).__name__}: {e}"[:200],
                                       elapsed=time.monotonic() - t0)
                elapsed = time.monotonic() - t0
                if resp.status_code in (429, 503):
                    wait = self._throttle(host, resp)
                    if attempt < self.retries and wait <= 60:
                        attempt += 1
                        continue
                    self.stats["errors"] += 1
                    return FetchResult(url, resp.status_code, elapsed=elapsed,
                                       headers=dict(resp.headers), error="throttled")
                if resp.status_code >= 500 and attempt < self.retries:
                    attempt += 1
                    await asyncio.sleep((2 ** attempt) + random.random())
                    continue
                # Quiet success: ease the gap back toward normal
                if resp.status_code < 400 and host.gap > host.base_gap:
                    host.gap = max(host.base_gap, host.gap * 0.9)
                if resp.status_code == 304:
                    self.stats["not_modified"] += 1
                self.stats["bytes"] += len(resp.content)
                return FetchResult(url, resp.status_code, resp.content, dict(resp.headers), elapsed)

    async def get(self, url: str, **kw) -> FetchResult:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, json: Any, **kw) -> FetchResult:
        return await self.request("POST", url, json=json, **kw)

    async def aclose(self) -> None:
        await self.client.aclose()
