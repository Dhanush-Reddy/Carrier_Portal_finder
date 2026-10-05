"""Identify the ATS behind a job portal from its URL.

Used by the discovery stage once a careers link has been followed to its
final URL. Patterns match on host (and path where the host is shared).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    ("Talentsoft", r"(^|\.)talent-soft\.com$", None),
    ("Dayforce", r"(^|\.)dayforcehcm\.com$", None),
    ("PageUp", r"(^|\.)pageuppeople\.com$", None),
    ("Kenexa BrassRing", r"(^|\.)brassring\.com$", None),
    ("softgarden", r"(^|\.)softgarden\.(io|de)$", None),
    ("rexx systems", r"(^|\.)rexx-systems\.com$", None),
    ("d.vinci", r"(^|\.)dvinci-hr\.com$|(^|\.)dvinci\.de$", None),
    ("Paylocity", r"(^|\.)paylocity\.com$", r"(?i)recruit"),
    ("JazzHR", r"(^|\.)applytojob\.com$", None),
    ("Breezy", r"(^|\.)breezy\.hr$", None),
    ("PeopleFluent", r"(^|\.)peopleclick\.com$|(^|\.)peoplefluent\.com$", None),
    ("Abacus Umantis", r"(^|\.)umantis\.com$", None),
    ("Jobylon", r"(^|\.)jobylon\.com$", None),
    ("Zoho Recruit", r"(^|\.)zohorecruit\.(com|eu|in)$", None),
    ("Moka", r"(^|\.)mokahr\.com$", None),
    ("Beisen", r"(^|\.)hotjob\.cn$|(^|\.)zhiye\.com$", None),
    ("Radancy", r"(^|\.)tmp\.com$|(^|\.)radancy\.net$|(^|\.)talentbrew\.com$", None),
    ("RippleHire", r"(^|\.)ripplehire\.com$", None),
    # Oracle Recruiting Cloud on the company's own domain (enterpriseplatform.dell.com).
    ("Oracle Recruiting Cloud", r".", r"^/hcmUI/CandidateExperience/"),
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
        first = urlsplit(clean_ats_url(url)).path.strip("/").lower()
        return f"{provider}:{first}"
    return f"{provider}:{host}"


ASSET_RE = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|pdf|docx?|xlsx?|pptx?|zip|mp4|mp3)$",
    re.IGNORECASE,
)

# Links on a careers site that are not a portal: sign-in and account pages,
# single job adverts, saved searches.
NOT_PORTAL_RE = re.compile(
    r"(log-?in|sign-?in|sign-?up|register|logout|/account|my-?account|myprofile|my-profile|"
    r"/profile|userhome|dashboard|saved-?jobs|saved-?searches|/applications|/applicant|"
    r"/user/|passport\.|mon-compte|navbarlevel=my_profile|jobalert|job-alert|"
    r"talentcommunity|talent-community|jobid=|job_id=|jobdetails|job-details|"
    r"/job/[^/]+|/jobs/\d|requisition|/req/|/vacancy/|/stelle/|/offre/|"
    r"/viewausschreibung/|applyonline|jobposting|/vacancies/\d|"
    # Recruitment-fraud warnings, blog posts, news, employee stories, equal
    # opportunity statements and career advice are linked from many careers
    # pages but are not portals.
    r"fraud|scam|phishing|recruitment-notice|[/-]blogs?/|/news[/-]|newsflash|/press/|press-releases?|"
    r"communiques|/magazine/|/articles?/|^stories\.|\.stories\.|/stories/|/meet-|"
    r"(?:^|[/_-])(?:eoe|eeo)|career-(?:resources|advice|coaching|tips)|toolkit|right-to-work|"
    # Legal pages.
    r"privacy|datenschutz|cookie|impressum|imprint|terms-of-use|terms-and-conditions)",
    re.IGNORECASE,
)
# A path segment this many words long is an article slug
# ("jf-announces-investment-plan-of-...-new-jobs"), not a portal page.
ARTICLE_SLUG_WORDS = 7


# Analytics and navigation-source parameters that do not change the page.
# (Not "cid": ADP uses it as the company id.)
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|_ga|_gl|gclid|fbclid|msclkid|mc_cid|mc_eid|igshid|yclid|icid|"
    r"intcmp|lnk|gnav|trk|jobpipeline|applysourceoverride|createnewalert)$", re.IGNORECASE)


def strip_tracking(url: str) -> str:
    """Remove analytics parameters (utm_*, _ga, gclid...) and the fragment."""
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


def is_asset(url: str) -> bool:
    return bool(ASSET_RE.search(urlsplit(url).path))


def is_not_portal(url: str) -> bool:
    """Sign-in, account, saved-search, single-job and article URLs."""
    parts = urlsplit(url)
    if NOT_PORTAL_RE.search(f"{parts.netloc}{parts.path}?{parts.query}"):
        return True
    return any(len(re.split(r"[-_]", seg)) >= ARTICLE_SLUG_WORDS
               for seg in parts.path.split("/"))


_WORKDAY_LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)
_ORACLE_SITE = re.compile(r"^(/hcmUI/CandidateExperience/[^/]+/sites/[^/]+)", re.IGNORECASE)


def clean_ats_url(url: str) -> str:
    """Reduce an ATS URL to its portal entry point.

    Drops login redirects and session parameters, e.g.
    ``.../AccentureCareers/login?redirect=...`` becomes ``.../AccentureCareers``.
    """
    provider = detect_ats(url)
    parts = urlsplit(url)
    if provider == "Workday" and "myworkday" in (parts.hostname or ""):
        segs = [s for s in parts.path.split("/") if s]
        keep = []
        for seg in segs:
            keep.append(seg)
            if not _WORKDAY_LOCALE.match(seg):
                break  # first non-locale segment is the career site name
        return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(keep), "", ""))
    if provider == "SuccessFactors" and parts.path.rstrip("/").endswith("/career"):
        params = dict(parse_qsl(parts.query))
        company = params.get("company") or params.get("career_company")
        if company:
            return urlunsplit((parts.scheme, parts.netloc, parts.path,
                               urlencode({"company": company}), ""))
    if provider == "Oracle Recruiting Cloud":
        # .../sites/CX_1/jobs?keyword=MBA and .../sites/CX_1001/tc-join are
        # pages of the site .../sites/CX_1.
        m = _ORACLE_SITE.match(parts.path)
        if m:
            return urlunsplit((parts.scheme, parts.netloc, m.group(1), "", ""))
    if provider in PATH_TENANT_PROVIDERS:
        # The portal is the tenant's board, e.g. https://jobs.lever.co/acme
        params = dict(parse_qsl(parts.query))
        if provider == "Greenhouse" and params.get("for"):
            return f"https://boards.greenhouse.io/{params['for']}"
        segs = [s for s in parts.path.split("/") if s]
        return urlunsplit((parts.scheme, parts.netloc, "/" + segs[0] if segs else "/", "", ""))
    if provider and is_not_portal(url):
        return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    return strip_tracking(url)
