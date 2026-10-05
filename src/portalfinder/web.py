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


MAX_RETRY_AFTER = 30.0  # seconds; a longer wait is not worth it for one page


def _retry_after(value: str | None) -> float:
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER) if value else 5.0
    except ValueError:  # an HTTP date; not worth parsing
        return 5.0


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
        # Origin -> parsed robots.txt, None (no rules) or a FetchError reason
        # when robots.txt could not be read and everything is disallowed.
        self._robots: dict[str, RobotFileParser | str | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _robots_for(self, origin: str) -> RobotFileParser | str | None:
        """robots.txt for an origin, read as RFC 9309 says.

        - 2xx: its rules apply.
        - 4xx, including 401 and 403: there are no rules. Bot firewalls often
          answer 403 here; the page itself then fails with its own status.
        - 5xx after a retry: everything is disallowed, recorded as
          ``robots_unreachable`` so it is not mistaken for a real block.
        - No answer after a retry: everything is disallowed too, recorded as
          ``timeout`` or ``site_unreachable`` since the site itself is down.
        """
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._robots:
                return self._robots[origin]
            result: RobotFileParser | str | None = "robots_unreachable"
            for _ in range(self.retries + 1):
                try:
                    resp = await self.client.get(origin + "/robots.txt")
                except httpx.TimeoutException:
                    result = "timeout"
                    continue
                except httpx.TransportError:
                    result = "site_unreachable"
                    continue
                except httpx.TooManyRedirects:
                    result = None  # RFC 9309: treated like a 4xx
                    break
                except httpx.HTTPError:
                    result = "robots_unreachable"
                    continue
                if resp.status_code >= 500:
                    result = "robots_unreachable"
                    continue
                if 200 <= resp.status_code < 300:
                    result = RobotFileParser()
                    result.parse(resp.text.splitlines())
                else:
                    result = None
                break
            self._robots[origin] = result
            return result

    async def sitemaps(self, origin: str) -> list[str]:
        """Sitemap URLs declared in the origin's robots.txt."""
        robots = await self._robots_for(origin)
        return list(robots.site_maps() or []) if isinstance(robots, RobotFileParser) else []

    async def blocked(self, url: str) -> str | None:
        """Why robots.txt forbids fetching ``url``, or None if it is allowed."""
        if not self.respect_robots:
            return None
        parts = urlsplit(url)
        robots = await self._robots_for(f"{parts.scheme}://{parts.netloc}")
        if isinstance(robots, str):
            return robots
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return "blocked_by_robots"
        return None

    async def allowed(self, url: str) -> bool:
        return await self.blocked(url) is None

    async def get(self, url: str) -> Page:
        try:
            httpx.URL(url)
        except (ValueError, httpx.InvalidURL) as exc:
            raise FetchError("invalid_url", url, str(exc)) from None
        reason = await self.blocked(url)
        if reason:
            raise FetchError(reason, url)
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
                    if attempt < self.retries:
                        if resp.status_code >= 500:
                            continue
                        if resp.status_code == 429:
                            await asyncio.sleep(_retry_after(resp.headers.get("retry-after")))
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
