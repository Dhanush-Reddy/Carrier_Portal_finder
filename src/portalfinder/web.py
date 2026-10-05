"""Polite async HTTP fetching: identifies itself, obeys robots.txt, caps size."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = (
    "portalfinder/0.1 (career portal research; "
    "+https://github.com/Dhanush-Reddy/Carrier_Portal_finder)"
)
MAX_BYTES = 3_000_000


@dataclass
class Page:
    requested_url: str
    url: str  # after redirects
    status: int
    text: str
    content_type: str

    @property
    def ok(self) -> bool:
        if not 200 <= self.status < 300:
            return False
        if "html" in self.content_type:
            return True
        # Some servers send HTML with a missing or generic content type.
        generic = not self.content_type or self.content_type.startswith(
            ("text/plain", "application/octet-stream"))
        return generic and self.text.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


class FetchError(Exception):
    """A fetch that could not complete. ``reason`` is a stable reason code."""

    def __init__(self, reason: str, url: str, detail: str = ""):
        super().__init__(f"{reason}: {url} {detail}".strip())
        self.reason = reason
        self.url = url
        self.detail = detail


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        retries: int = 1,
        respect_robots: bool = True,
    ):
        self.client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self.retries = retries
        self.respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _robots_for(self, origin: str) -> RobotFileParser | None:
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._robots:
                return self._robots[origin]
            parser: RobotFileParser | None = None
            try:
                resp = await self.client.get(origin + "/robots.txt")
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                elif resp.status_code in (401, 403):
                    # Per RFC 9309, an access error on robots.txt means disallow all.
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
            except httpx.HTTPError:
                parser = None  # unreachable robots.txt: treat as no restrictions
            self._robots[origin] = parser
            return parser

    async def sitemaps(self, origin: str) -> list[str]:
        """Sitemap URLs declared in the origin's robots.txt."""
        robots = await self._robots_for(origin)
        return list(robots.site_maps() or []) if robots else []

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        robots = await self._robots_for(f"{parts.scheme}://{parts.netloc}")
        return robots is None or robots.can_fetch(USER_AGENT, url)

    async def get(self, url: str) -> Page:
        if not await self.allowed(url):
            raise FetchError("blocked_by_robots", url)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                async with self.client.stream("GET", url) as resp:
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if resp.status_code >= 500 and attempt < self.retries:
                        continue
                    return Page(
                        requested_url=url,
                        url=str(resp.url),
                        status=resp.status_code,
                        text=body.decode(resp.encoding or "utf-8", errors="replace"),
                        content_type=resp.headers.get("content-type", "").lower(),
                    )
            except httpx.TimeoutException as exc:
                last = exc
                reason = "timeout"
            except httpx.TransportError as exc:
                last = exc
                reason = "site_unreachable"
            except httpx.HTTPError as exc:
                last = exc
                reason = "http_error"
        raise FetchError(reason, url, str(last))
