"""Normalisation helpers used for matching records across sources."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_LINKEDIN_RE = re.compile(r"linkedin\.com/company/([^/?#]+)", re.IGNORECASE)


def website_domain(url: str | None) -> str | None:
    """Return the host of ``url`` without ``www.``, lower-cased."""
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def linkedin_company_url(value: str | None) -> str | None:
    """Canonical LinkedIn company URL from a URL or a bare company slug/ID."""
    if not value:
        return None
    value = value.strip()
    match = _LINKEDIN_RE.search(value)
    slug = match.group(1) if match else value.strip("/")
    if not slug or "/" in slug or " " in slug:
        return None
    return f"https://www.linkedin.com/company/{slug.lower()}"
