"""Find each company's career portals, starting from its official website.

For one company:

1. Fetch the homepage and collect links that look like careers links.
2. If there are none, probe common locations (``/careers``, ``careers.<domain>``).
3. Fetch the best candidate as the main careers page, and look on it for a
   link into an ATS (Workday, Greenhouse, ...). Fetch that link to get the
   final ATS URL.
4. Treat other careers links on the homepage or main careers page as extra
   portals when they point to an ATS, to another site, or to a graduate or
   regional page. Resolve each one the same way.

Every company processed ends with a status and, unless portals were found, a
reason code. Nothing is skipped: an unexpected error is recorded as
``failed`` / ``internal_error``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import traceback
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import tldextract
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from portalfinder.ats import (
    PATH_TENANT_PROVIDERS,
    ats_tenant,
    clean_ats_url,
    detect_ats,
    is_asset,
    is_not_portal,
    strip_tracking,
)
from portalfinder.db import log_event, now
from portalfinder.terms import (
    EXCLUDED_DOMAIN_NAMES,
    EXCLUDED_HOSTS,
    GRADUATE_RE,
    JOB_CONTENT_RE,
    has_career_text,
    has_career_url,
    name_matches,
    region_of,
)
from portalfinder.web import Fetcher, FetchError, Page

_extract = tldextract.TLDExtract(suffix_list_urls=())  # bundled list, no network

# Extra portals kept per company, by scope. Regional pages are often many
# (one per country) and all worth keeping; the others are capped tighter.
SCOPE_CAPS = {"regional": 30, "graduate": 3, "affiliate": 5, "other": 3}
# Link text longer than this is a job title or teaser, not a region label.
MAX_REGION_TEXT = 60
# Graduate wording inside URLs, where words run together ("ATTcollege").
GRADUATE_URL_RE = re.compile(
    r"college|graduate|campus|internship|student|early-?careers?|apprentic|high-?school",
    re.IGNORECASE)
# ATS tenants used for testing, never the live portal.
SANDBOX_RE = re.compile(r"sandbox|staging|[-.]uat[-.]|[-.]test[-.]|preprod", re.IGNORECASE)
INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
# First path segments of ATS script URLs that are not a company's board
# (apply.app.jobvite.com/assets/...).
NOT_TENANTS = {"", "embed", "assets", "static", "js", "css", "cdn", "scripts", "widget", "widgets", "api"}
PROBE_PATHS = ("/careers", "/jobs", "/en/careers", "/career", "/about/careers",
               "/about-us/careers", "/company/careers", "/en/about/careers", "/join-us",
               "/work-with-us", "/en/jobs", "/recruit", "/karriere")
MAX_SITEMAPS = 4  # sitemap files read per company (index + children)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
PROBE_SUBDOMAINS = ("careers", "jobs")

# Confidence weights; see ``score``.
WEIGHTS = {
    "linked_from_official_site": 0.30,
    "found_by_probe": 0.20,
    "on_company_domain": 0.20,
    "ats_detected": 0.20,
    "name_match": 0.15,
    "job_content": 0.15,
}


def registrable_domain(url_or_host: str) -> str:
    ext = _extract(url_or_host)
    return ext.top_domain_under_public_suffix or ext.domain


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


# Query parameters that pick a language version of a page, not another page.
LANGUAGE_PARAMS = {"locale", "lang", "language", "hl", "userlocation"}


def page_key(url: str) -> str:
    """The same for every variant of one page.

    Ignores the scheme, tracking parameters and language parameters, so
    ``?locale=de_DE`` or ``?userlocation=gb`` versions of a careers page
    count as that page. Other parameters can make a different page
    (``search-jobs?location=...``) and are kept.
    """
    parts = urlsplit(clean_ats_url(url) if detect_ats(url) else strip_tracking(url))
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if k.lower() not in LANGUAGE_PARAMS))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", parts.netloc.lower(), path, query, ""))


@dataclass
class Link:
    url: str
    text: str
    kind: str  # a | iframe | script | form


@dataclass
class ParsedPage:
    title: str
    links: list[Link]
    text: str


def parse(page: Page) -> ParsedPage:
    tree = HTMLParser(page.text)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    links: list[Link] = []
    for selector, attr, kind in (
        ("a[href]", "href", "a"),
        ("iframe[src]", "src", "iframe"),
        ("script[src]", "src", "script"),
        ("form[action]", "action", "form"),
    ):
        for node in tree.css(selector):
            raw = (node.attributes.get(attr) or "").strip()
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            try:
                url = urljoin(page.url, raw)
                httpx.URL(url)  # rejects hosts that can't be requested ("http://[x")
            except (ValueError, httpx.InvalidURL):
                continue
            if not url.startswith(("http://", "https://")):
                continue
            text = ""
            if kind == "a":
                text = (
                    node.text(deep=True, separator=" ", strip=True)
                    or node.attributes.get("aria-label")
                    or node.attributes.get("title")
                    or ""
                )
            links.append(Link(url, text[:200], kind))
    body = tree.body.text(separator=" ", strip=True)[:200_000] if tree.body else ""
    return ParsedPage(title, links, body)


def _excluded(url: str) -> bool:
    """Third-party job boards, social sites, app stores and government sites."""
    host = (urlsplit(url).hostname or "").lower()
    ext = _extract(host)
    return host in EXCLUDED_HOSTS or ext.domain in EXCLUDED_DOMAIN_NAMES


def _is_government(url: str) -> bool:
    ext = _extract(urlsplit(url).hostname or "")
    return ext.suffix.split(".")[0] in ("gov", "mil", "gob", "gouv") or ext.domain == "gov"


def _valid_host(url: str) -> bool:
    """A real public host, not ``www.`` from a broken link."""
    return bool(_extract(urlsplit(url).hostname or "").suffix)


def is_careers_link(link: Link) -> bool:
    if (link.kind != "a" or not _valid_host(link.url) or _excluded(link.url)
            or is_asset(link.url) or is_not_portal(link.url) or SANDBOX_RE.search(link.url)):
        return False
    parts = urlsplit(link.url)
    return (
        has_career_text(link.text)
        or has_career_url(parts.hostname or "", parts.path)
        or detect_ats(link.url) is not None
    )


def link_score(link: Link) -> float:
    """Rank careers links; the highest becomes the main careers page."""
    parts = urlsplit(link.url)
    text = link.text.strip().lower()
    score = 0.0
    if text in {"careers", "career", "jobs", "join us", "work with us", "karriere",
                "carrières", "carreras", "empleo", "careers & jobs", "jobs & careers"}:
        score += 3
    elif has_career_text(link.text):
        score += 2
    if has_career_url(parts.hostname or "", parts.path):
        score += 2
    if (parts.hostname or "").split(".")[0] in PROBE_SUBDOMAINS:
        score += 1
    if detect_ats(link.url):
        score += 1.5
    if GRADUATE_RE.search(link.text) or GRADUATE_RE.search(parts.path.replace("-", " ")):
        score -= 1.5
    if region_of(link.text, parts.path):
        score -= 1
    if len([s for s in parts.path.split("/") if s]) <= 2:
        score += 0.5
    return score


def classify_scope(link_text: str, url: str, company_domains: set[str]) -> tuple[str, str | None]:
    parts = urlsplit(url)
    region_text = link_text if len(link_text) <= MAX_REGION_TEXT else ""
    if GRADUATE_RE.search(link_text) or GRADUATE_RE.search(parts.path.replace("-", " ")):
        return "graduate", region_of(region_text, parts.path)
    region = region_of(region_text, parts.path)
    if region:
        return "regional", region
    if company_domains and not detect_ats(url) and registrable_domain(url) not in company_domains:
        return "affiliate", None
    return "other", None


def _shared_path(url: str) -> bool:
    """A provider-wide path such as apply.app.jobvite.com/assets, not a board."""
    tenant = ats_tenant(url) or ""
    return (detect_ats(url) in PATH_TENANT_PROVIDERS
            and tenant.split(":", 1)[1] in NOT_TENANTS - {""})


def find_ats_link(parsed: ParsedPage, company_name: str = "") -> str | None:
    """The best link into an ATS on a page.

    Job-search links beat sign-in, profile and single-job links; anchors beat
    iframes and forms; boards named after the company beat others. Scripts,
    static files and test (sandbox) boards are never portals.

    A group page that links several companies' boards, none named after the
    group and none labelled as a job search (Grupo ACS linking Turner's and
    others'), has no board of its own: None, so no subsidiary's board is
    reported as the group's.
    """
    ats_links = [
        l for l in parsed.links
        if l.kind != "script" and not is_asset(l.url) and detect_ats(l.url)
        and not SANDBOX_RE.search(l.url) and not _shared_path(l.url)
    ]
    if not ats_links:
        return None

    def named(l: Link) -> bool:
        return bool(company_name) and name_matches(company_name, l.url)

    def job_text(l: Link) -> bool:
        return bool(JOB_CONTENT_RE.search(l.text or "") or has_career_text(l.text or ""))

    def graduate(l: Link) -> bool:
        return bool(GRADUATE_RE.search(l.text or "") or GRADUATE_URL_RE.search(l.url))

    # A graduate board next to the main one is normal, so it doesn't count here.
    main_links = [l for l in ats_links if not is_not_portal(l.url) and not graduate(l)]
    if (company_name and len({ats_tenant(l.url) for l in main_links}) >= 2
            and not any(named(l) or job_text(l) for l in main_links)):
        return None
    ats_links.sort(key=lambda l: (
        is_not_portal(l.url),
        # A graduate-only board is not the main one (AT&T's ATTcollege).
        graduate(l),
        l.kind != "a",
        not named(l),
        not job_text(l),
    ))
    return ats_links[0].url


def ats_from_scripts(parsed: ParsedPage) -> tuple[str, str | None] | None:
    """The ATS a careers site runs on, from the scripts it loads.

    Hosted career sites (Phenom, SuccessFactors, Avature...) often sit on the
    company's own domain and only reveal the provider through their scripts.
    Returns ``(provider, board_url)``; ``board_url`` is set for embedded
    boards that name their tenant, e.g. Greenhouse's ``embed/job_board/js?for=acme``.
    """
    for link in parsed.links:
        if link.kind == "script" and not SANDBOX_RE.search(link.url):
            provider = detect_ats(link.url)
            if provider:
                board = None
                if provider in PATH_TENANT_PROVIDERS:
                    tenant = ats_tenant(link.url) or ""
                    if tenant.split(":", 1)[1] not in NOT_TENANTS:
                        board = clean_ats_url(link.url)
                return provider, board
    return None


@dataclass
class PortalFound:
    career_page_url: str
    scope: str
    region: str | None = None
    final_ats_url: str | None = None
    ats_provider: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    discovered_via: str = "homepage_link"
    signals: dict = field(default_factory=dict)
    fetch_error: str | None = None
    ats_link: str | None = None  # the ATS link as found, before redirects
    final_page_url: str | None = None
    ats_found_via: str | None = None  # page_url | link | page_scripts
    link_url: str | None = None  # the link as the site gave it, tracking params included

    @property
    def confidence(self) -> float:
        return round(min(1.0, sum(WEIGHTS[k] for k, v in self.signals.items() if v)), 2)


@dataclass
class DiscoveryResult:
    company_id: int
    status: str
    reason: str | None = None
    portals: list[PortalFound] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class Company:
    id: int
    name: str
    website: str | None


class CompanyDiscovery:
    def __init__(self, fetcher: Fetcher, company: Company):
        self.fetcher = fetcher
        self.company = company
        # Invisible characters (soft hyphens) sometimes come with source data.
        website = INVISIBLE_RE.sub("", company.website or "").strip()
        if website and "://" not in website:
            website = "https://" + website
        self.website = website or None
        self.domain = registrable_domain(self.website) if self.website else None
        self.domains = {self.domain} if self.domain else set()

    async def _fetch(self, url: str) -> tuple[Page | None, str | None]:
        try:
            page = await self.fetcher.get(url)
        except FetchError as exc:
            return None, exc.reason
        if not page.ok:
            return page, f"http_{page.status}" if page.status >= 300 else "not_html"
        return page, None

    async def _resolve(self, portal: PortalFound, link_text: str) -> ParsedPage | None:
        """Fetch a portal page, find its ATS link and fill in the signals."""
        page, error = await self._fetch(portal.link_url or portal.career_page_url)
        on_ats = detect_ats(portal.career_page_url)
        if page is not None:
            portal.http_status = page.status
        if error:
            portal.fetch_error = error
            if on_ats:
                portal.final_ats_url = portal.career_page_url
                portal.ats_provider = on_ats
            portal.signals.update(
                ats_detected=bool(on_ats),
                on_company_domain=self._on_company_domain(portal.career_page_url),
                name_match=name_matches(self.company.name, portal.career_page_url, link_text),
            )
            return None
        parsed = parse(page)
        portal.page_title = parsed.title[:300]
        final_page_url = portal.final_page_url = page.url
        ats_via = None
        if detect_ats(final_page_url) and not SANDBOX_RE.search(final_page_url):
            ats_url, ats_via = final_page_url, "page_url"
        else:
            ats_url = find_ats_link(parsed, self.company.name)
            ats_via = "link" if ats_url else None
        if ats_url and ats_url != final_page_url:
            portal.ats_link = ats_url
            ats_page, ats_error = await self._fetch(ats_url)
            if (ats_page is not None and not ats_error and detect_ats(ats_page.url)
                    and not SANDBOX_RE.search(ats_page.url)):
                ats_url = ats_page.url
        if ats_url:
            portal.final_ats_url = clean_ats_url(ats_url)
            portal.ats_provider = detect_ats(portal.final_ats_url)
        else:
            # A hosted career site on the company's domain is itself the portal,
            # unless its scripts embed a named board (Greenhouse, Lever...).
            found = ats_from_scripts(parsed)
            portal.ats_provider = found[0] if found else None
            portal.final_ats_url = (found[1] if found and found[1] else None) or strip_tracking(final_page_url)
            ats_via = "page_scripts" if found else None
        portal.ats_found_via = ats_via
        portal.signals.update(
            ats_detected=portal.ats_provider is not None,
            on_company_domain=self._on_company_domain(final_page_url),
            name_match=name_matches(
                self.company.name, parsed.title, portal.final_ats_url or "", final_page_url
            ),
            job_content=bool(ats_url) or bool(JOB_CONTENT_RE.search(parsed.text)),
        )
        return parsed

    def _on_company_domain(self, url: str) -> bool:
        return registrable_domain(url) in self.domains

    async def _sitemap_candidates(self, origin: str) -> list[str]:
        """Careers URLs listed in the site's sitemap(s), shortest path first.

        Helps when the homepage builds its menu with JavaScript or hides the
        careers link, since sitemaps list pages regardless.
        """
        queue = await self.fetcher.sitemaps(origin) or [origin + "/sitemap.xml"]
        found: list[str] = []
        read = 0
        while queue and read < MAX_SITEMAPS:
            url = queue.pop(0)
            if url.endswith(".gz"):
                continue
            read += 1
            try:
                page = await self.fetcher.get(url)
            except FetchError:
                continue
            if page.status != 200:
                continue
            locs = _LOC_RE.findall(page.text)
            if "<sitemapindex" in page.text[:2000].lower():
                # Read child sitemaps that look careers-related first.
                locs.sort(key=lambda u: not has_career_url("", urlsplit(u).path))
                queue.extend(locs)
                continue
            for loc in locs:
                parts = urlsplit(loc)
                if (has_career_url(parts.hostname or "", parts.path)
                        and not is_not_portal(loc) and not is_asset(loc)):
                    found.append(loc)
        found.sort(key=lambda u: (len([s for s in urlsplit(u).path.split("/") if s]), len(u)))
        return found[:3]

    async def _probe(self, same_origin: bool = True) -> tuple[PortalFound, str] | None:
        """Try the sitemap, then common careers locations. ``same_origin=False``
        skips the homepage's host, used when that host is down."""
        base = urlsplit(self.website)
        origin = f"{base.scheme or 'https'}://{base.netloc or self.domain}"
        candidates: list[tuple[str, str]] = []
        if same_origin:
            candidates += [(u, "sitemap") for u in await self._sitemap_candidates(origin)]
            candidates += [(origin + p, "path_probe") for p in PROBE_PATHS]
        candidates += [(f"https://{sub}.{self.domain}/", "path_probe") for sub in PROBE_SUBDOMAINS]
        home = normalize_url(self.website)
        tried: set[str] = set()
        for url, via in candidates:
            if normalize_url(url) in tried:
                continue
            tried.add(normalize_url(url))
            page, error = await self._fetch(url)
            if error or page is None or normalize_url(page.url) == home:
                continue
            parsed = parse(page)
            if has_career_text(parsed.title) or JOB_CONTENT_RE.search(parsed.text[:20_000]):
                return PortalFound(strip_tracking(url), "global", link_url=url, discovered_via=via,
                                   signals={"found_by_probe": True}), parsed.title
        return None

    async def run(self) -> DiscoveryResult:
        cid = self.company.id
        if not self.website:
            return DiscoveryResult(cid, "no_portal_found", "no_website")

        home_page, home_error = await self._fetch(self.website)
        home = parse(home_page) if home_page is not None and not home_error else None
        if home_page is not None:
            self.domains.add(registrable_domain(home_page.url))
        candidates = [l for l in home.links if is_careers_link(l)] if home else []

        main: PortalFound | None = None
        main_text = ""
        if candidates:
            best = max(candidates, key=link_score)
            main_text = best.text
            main = PortalFound(strip_tracking(best.url), "global", link_url=best.url,
                               discovered_via="homepage_link",
                               signals={"linked_from_official_site": True})
        elif self.domain:
            host_down = home_error in ("site_unreachable", "timeout")
            probed = await self._probe(same_origin=not host_down)
            if probed:
                main, main_text = probed

        if main is None:
            if home is None:
                return DiscoveryResult(cid, "failed", home_error or "homepage_unreachable",
                                       detail={"url": self.website})
            reason = "no_links_in_html" if len(home.links) < 5 else "no_careers_link"
            return DiscoveryResult(cid, "no_portal_found", reason,
                                   detail={"links_on_homepage": len(home.links)})

        main_parsed = await self._resolve(main, main_text)
        portals = [main]
        # The main careers site is the company's own even when it has its own
        # domain (amazon.jobs, verbund.edeka), so its pages are not affiliates.
        # That holds even when it could not be fetched (career.abchina.com.cn).
        for url in (main.career_page_url, main.final_page_url):
            if url and not detect_ats(url):
                self.domains.add(registrable_domain(url))

        # Extra portals: from the homepage and the main careers page.
        seen = {page_key(u) for u in (main.career_page_url, main.final_ats_url, main.ats_link) if u}
        tenants = {t for t in map(ats_tenant, (main.career_page_url, main.final_ats_url, main.ats_link)) if t}
        pool = list(candidates)
        # Links on a page that is itself an ATS are job listings, not more portals.
        if main_parsed and not detect_ats(main.final_page_url):
            pool += [l for l in main_parsed.links if is_careers_link(l)]
        extras: list[tuple[Link, str, str | None]] = []
        kept_paths: dict[str, list[str]] = {}
        skipped: dict[str, int] = {}
        for link in pool:
            key = page_key(link.url)
            tenant = ats_tenant(link.url)
            if key in seen or (tenant and tenant in tenants):
                continue  # already covered, e.g. a job link into the main ATS
            scope, region = classify_scope(link.text, link.url, self.domains)
            if scope == "other" and not detect_ats(link.url):
                continue  # e.g. /careers/benefits: part of the main site, not a portal
            if scope == "affiliate" and _is_government(link.url):
                continue  # e.g. a job-scam warning on a consumer-protection site
            seen.add(key)
            # A page nested under one already kept for the same scope is a
            # sub-page of it (e.g. /internships/finance under /internships).
            if any(key.startswith(p + "/") for p in kept_paths.get(scope, [])):
                continue
            if sum(1 for _, s, _ in extras if s == scope) >= SCOPE_CAPS[scope]:
                skipped[scope] = skipped.get(scope, 0) + 1
                continue
            if tenant:
                tenants.add(tenant)
            kept_paths.setdefault(scope, []).append(key)
            extras.append((link, scope, region))
        # Pages reached after redirects, so that /europe -> /en/europe is not
        # kept next to /en/europe, nor bank.sbi/careers -> the main page.
        landed = {page_key(u) for u in (main.career_page_url, main.final_page_url) if u}
        duplicates = 0
        for link, scope, region in extras:
            portal = PortalFound(strip_tracking(link.url), scope, region, link_url=link.url,
                                 discovered_via="careers_page_link",
                                 signals={"linked_from_official_site": True})
            await self._resolve(portal, link.text)
            if portal.final_page_url and page_key(portal.final_page_url) in landed:
                duplicates += 1
                continue
            landed.update(page_key(u) for u in (portal.career_page_url, portal.final_page_url) if u)
            portals.append(portal)

        detail = {"extra_portals_over_cap": skipped} if skipped else {}
        if duplicates:
            detail["extra_portals_same_page"] = duplicates
        if main.fetch_error:
            return DiscoveryResult(cid, "needs_review", "careers_page_unreachable",
                                   portals, {**detail, "error": main.fetch_error})
        return DiscoveryResult(cid, "discovered", None, portals, detail)


async def discover_company(fetcher: Fetcher, company: Company) -> DiscoveryResult:
    try:
        return await CompanyDiscovery(fetcher, company).run()
    except Exception as exc:  # recorded, never swallowed silently
        return DiscoveryResult(company.id, "failed", "internal_error", detail={
            "error": repr(exc), "trace": traceback.format_exc(limit=5),
        })


def save_result(conn: sqlite3.Connection, result: DiscoveryResult) -> None:
    ts = now()
    removed = conn.execute(
        "DELETE FROM career_portals WHERE company_id = ? AND discovered_via != 'manual'",
        (result.company_id,),
    ).rowcount
    for p in result.portals:
        conn.execute(
            "INSERT INTO career_portals (company_id, career_page_url, final_ats_url,"
            " ats_provider, scope, region, verification_status, confidence, discovered_via,"
            " page_title, http_status, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?, 'unverified', ?, ?, ?, ?, ?)"
            " ON CONFLICT (company_id, career_page_url) DO NOTHING",
            (result.company_id, p.career_page_url, p.final_ats_url, p.ats_provider, p.scope,
             p.region, p.confidence, p.discovered_via, p.page_title, p.http_status,
             json.dumps({"signals": p.signals, "fetch_error": p.fetch_error,
                         "ats_found_via": p.ats_found_via})),
        )
    conn.execute(
        "UPDATE companies SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
        (result.status, result.reason, ts, result.company_id),
    )
    log_event(conn, "discover", result.status, {
        "reason": result.reason, "portals": len(result.portals),
        "replaced_portals": removed, **result.detail,
    }, company_id=result.company_id)


@dataclass
class DiscoverySummary:
    selected: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    @property
    def accounted(self) -> int:
        return sum(self.by_status.values())


class DiscoveryReconciliationError(RuntimeError):
    pass


async def discover_all(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    statuses: tuple[str, ...] = ("pending",),
    limit: int | None = None,
    concurrency: int = 10,
    company_ids: list[int] | None = None,
    on_result=None,
) -> DiscoverySummary:
    sql = "SELECT id, name, website FROM companies WHERE status IN ({})".format(
        ",".join("?" for _ in statuses))
    params: list = list(statuses)
    if company_ids:
        sql += " AND id IN ({})".format(",".join("?" for _ in company_ids))
        params += company_ids
    sql += " ORDER BY employee_count DESC, id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    companies = [Company(r["id"], r["name"], r["website"]) for r in conn.execute(sql, params)]

    summary = DiscoverySummary(selected=len(companies))
    sem = asyncio.Semaphore(concurrency)

    async def worker(company: Company) -> DiscoveryResult:
        async with sem:
            return await discover_company(fetcher, company)

    tasks = [asyncio.create_task(worker(c)) for c in companies]
    for i, fut in enumerate(asyncio.as_completed(tasks), 1):
        result = await fut
        save_result(conn, result)
        summary.by_status[result.status] = summary.by_status.get(result.status, 0) + 1
        if on_result:
            on_result(result)
        if i % 50 == 0:
            conn.commit()
    conn.commit()
    if summary.accounted != summary.selected:
        raise DiscoveryReconciliationError(
            f"selected {summary.selected} companies but recorded {summary.accounted} results"
        )
    return summary
