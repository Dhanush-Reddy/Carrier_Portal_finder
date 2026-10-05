"""Identify the ATS behind a job portal from its URL.

Used by the discovery stage once a careers link has been followed to its
final URL. Patterns match on host (and path where the host is shared).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# (provider, host regex, optional path regex). First match wins.
PATTERNS: list[tuple[str, str, str | None]] = [
    ("Workday", r"(^|\.)myworkdayjobs\.com$|(^|\.)myworkdaysite\.com$", None),
    ("Workday", r"(^|\.)workday\.com$", r"^/[^/]+/d/"),
    ("Greenhouse", r"(^|\.)greenhouse\.io$", None),
    ("Lever", r"(^|\.)lever\.co$", None),
    ("SuccessFactors", r"(^|\.)successfactors\.(com|eu)$|(^|\.)sapsf\.(com|eu|cn)$", None),
    ("SuccessFactors", r"^jobs\.sap\.com$|(^|\.)jobs\.hr\.cloud\.sap$", None),
    ("iCIMS", r"(^|\.)icims\.com$", None),
    ("Oracle/Taleo", r"(^|\.)taleo\.net$", None),
    ("Oracle Recruiting Cloud", r"(^|\.)oraclecloud\.com$", r"hcmUI/CandidateExperience"),
    ("SmartRecruiters", r"(^|\.)smartrecruiters\.com$", None),
    ("Eightfold", r"(^|\.)eightfold\.ai$", None),
    ("Phenom", r"(^|\.)phenompeople\.com$", None),
    ("Avature", r"(^|\.)avature\.net$", None),
    ("Jobvite", r"(^|\.)jobvite\.com$", None),
    ("BambooHR", r"(^|\.)bamboohr\.com$", None),
    ("Ashby", r"(^|\.)ashbyhq\.com$", None),
    ("Workable", r"(^|\.)workable\.com$", None),
    ("Recruitee", r"(^|\.)recruitee\.com$", None),
    ("Teamtailor", r"(^|\.)teamtailor\.com$", None),
    ("Personio", r"(^|\.)jobs\.personio\.(de|com)$", None),
    ("Cornerstone", r"(^|\.)csod\.com$", None),
    ("UKG", r"(^|\.)ultipro\.com$|(^|\.)ukg\.net$", None),
    ("ADP", r"(^|\.)adp\.com$", r"(?i)recruit|career|jobs"),
    ("Jobs2Web/SAP", r"(^|\.)jobs2web\.com$", None),
    ("Paradox", r"(^|\.)paradox\.ai$", None),
    ("Beamery", r"(^|\.)beamery\.com$", None),
]

_COMPILED = [
    (name, re.compile(host), re.compile(path) if path else None)
    for name, host, path in PATTERNS
]


def detect_ats(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    for name, host_re, path_re in _COMPILED:
        if host_re.search(host) and (path_re is None or path_re.search(parts.path)):
            return name
    return None


# Providers that host many companies on one host and tell them apart by the
# first path segment (boards.greenhouse.io/<company>). The rest give each
# company its own host (<company>.wd5.myworkdayjobs.com).
PATH_TENANT_PROVIDERS = {"Greenhouse", "Lever", "SmartRecruiters", "Ashby", "Workable", "Jobvite"}


def ats_tenant(url: str | None) -> str | None:
    """A key that is the same for every URL of one company's ATS portal."""
    provider = detect_ats(url)
    if not provider:
        return None
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    if provider in PATH_TENANT_PROVIDERS:
        first = next((s for s in parts.path.lower().split("/") if s), "")
        return f"{provider}:{first}"
    return f"{provider}:{host}"
