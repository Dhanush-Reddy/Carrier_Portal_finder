import asyncio
import json

import httpx
import pytest

from portalfinder import discover as disc
from portalfinder.discover import (
    Company, Link, classify_scope, discover_all, link_score, registrable_domain,
)
from portalfinder.export import export_csv
from portalfinder.ingest import ingest
from portalfinder.records import CompanyRecord
from portalfinder.report import build_report
from portalfinder.terms import name_matches, region_of
from portalfinder.web import Fetcher

FILLER = "".join(f'<a href="/page{i}">Page {i}</a>' for i in range(6))


def html(title, body=""):
    return f"<html><head><title>{title}</title></head><body>{FILLER}{body}</body></html>"


# url -> (status, body, extra headers). Unknown hosts fail to connect;
# unknown paths on known hosts are 404.
SITE = {
    # Acme: careers page with Workday, plus graduate, regional and ATS extras.
    "https://www.acme.com/": (200, html("Acme", '<a href="/careers">Careers</a>'
        '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>'), {}),
    "https://www.acme.com/careers": (200, html("Careers at Acme",
        '<p>Search jobs across our teams.</p>'
        '<a href="https://acme.wd5.myworkdayjobs.com/External">Search jobs</a>'
        '<a href="/careers/graduates">Graduate programme</a>'
        '<a href="https://careers.acme.in/">Careers in India</a>'
        '<a href="/careers/benefits">Careers benefits</a>'
        '<a href="https://jobs.lever.co/acmelabs">Acme Labs jobs</a>'
        '<a href="https://acme.wd5.myworkdayjobs.com/External/job/123">Engineer</a>'), {}),
    "https://acme.wd5.myworkdayjobs.com/External": (
        302, "", {"location": "https://acme.wd5.myworkdayjobs.com/en-US/External"}),
    "https://acme.wd5.myworkdayjobs.com/en-US/External": (200, html("Acme jobs"), {}),
    "https://www.acme.com/careers/graduates": (200, html("Graduates",
        '<a href="https://boards.greenhouse.io/acmegrads">Apply now</a>'), {}),
    "https://boards.greenhouse.io/acmegrads": (200, html("Acme graduate jobs"), {}),
    "https://careers.acme.in/": (200, html("Acme India careers",
        '<a href="https://acme.taleo.net/careersection/in/jobsearch.ftl">View jobs</a>'), {}),
    "https://acme.taleo.net/careersection/in/jobsearch.ftl": (200, html("Taleo"), {}),
    "https://jobs.lever.co/acmelabs": (200, html("Acme Labs"), {}),
    # Beta: no careers link, but /jobs exists.
    "https://beta.com/": (200, html("Beta"), {}),
    "https://beta.com/jobs": (200, html("Jobs at Beta", "<p>See our open positions</p>"), {}),
    # Gamma: no careers link; /careers just redirects home (soft 404).
    "https://gamma.com/": (200, html("Gamma"), {}),
    "https://gamma.com/careers": (301, "", {"location": "https://gamma.com/"}),
    # Epsilon: robots.txt disallows everything.
    "https://epsilon.com/robots.txt": (200, "User-agent: *\nDisallow: /", {"content-type": "text/plain"}),
    "https://epsilon.com/": (200, html("Epsilon", '<a href="/careers">Careers</a>'), {}),
    # Eta: careers link goes straight to an ATS.
    "https://eta.io/": (200, html("Eta", '<a href="https://jobs.lever.co/eta">Join us</a>'), {}),
    "https://jobs.lever.co/eta": (200, html("Eta - Jobs",
        '<a href="https://jobs.lever.co/eta/abc-123">Software Engineer jobs</a>'), {}),
    # Theta: careers site blocks bots.
    "https://theta.com/": (200, html("Theta", '<a href="https://careers.theta.com/">Careers</a>'), {}),
    "https://careers.theta.com/": (403, "Forbidden", {}),
    "https://kappa.com/": (200, html("Kappa", '<a href="/careers">Careers</a>'), {}),
    "https://kappa.com/careers": (200, html("Kappa careers", "<p>Open roles</p>"), {}),
    # Lambda: hosted (Phenom) career site that only shows the ATS in its
    # scripts, plus links that are not portals (sign-in, job advert, PDF).
    "https://lambda.com/": (200, html("Lambda", '<a href="https://careers.lambda.com/global/en/">Careers</a>'), {}),
    "https://careers.lambda.com/global/en/": (200, html("Lambda careers",
        '<script src="https://cdn.phenompeople.com/x/bluebird.min-1.0.js"></script>'
        '<p>Search jobs</p>'
        '<a href="/global/en/students-graduates">Students & graduates</a>'
        '<a href="/global/en/students-graduates/internships">Internships</a>'
        '<a href="/global/en/lambda-usa">Lambda USA careers</a>'
        '<a href="/global/en/login">Sign in to careers</a>'
        '<a href="/global/en/saved-jobs">Saved jobs</a>'
        '<a href="/global/en/job/12345/Driver-India">Driver jobs India</a>'
        '<a href="https://cdn.lambda.com/eeo.pdf">Careers EEO statement</a>'), {}),
    "https://careers.lambda.com/global/en/students-graduates": (200, html("Graduates"), {}),
    "https://careers.lambda.com/global/en/lambda-usa": (200, html("USA"), {}),
    # Mu: careers site on its own domain, Workday sign-in link before the search link.
    "https://mu.com/": (200, html("Mu", '<a href="https://mu.jobs/">Careers</a>'), {}),
    "https://mu.jobs/": (200, html("Mu jobs",
        '<a href="https://mu.wd1.myworkdayjobs.com/MuCareers/login?redirect=%2FMuCareers%2FuserHome">Sign in</a>'
        '<a href="https://mu.wd1.myworkdayjobs.com/MuCareers">Search jobs</a>'
        '<a href="/en/locations">Locations</a><a href="/en/teams">Teams</a>'), {}),
    "https://mu.wd1.myworkdayjobs.com/MuCareers": (200, html("Mu Careers"), {}),
    # Nu Post: a government-owned company whose own site is on gov.in.
    "https://nupost.gov.in/": (200, html("Nu Post", '<a href="/careers">Careers</a>'), {}),
    "https://nupost.gov.in/careers": (200, html("Nu Post careers",
        '<p>Current openings</p><a href="https://www.usajobs.gov/">Federal jobs</a>'), {}),
    # Omicron: tracking parameters, an embedded Greenhouse board, a scam warning.
    "https://omicron.com/": (200, html("Omicron",
        '<a href="https://jobs.omicron.com/fr?utm_source=omicron.com&utm_medium=web">Careers</a>'), {}),
    "https://jobs.omicron.com/fr?utm_source=omicron.com&utm_medium=web": (200, html("Omicron jobs",
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=omicron"></script>'
        '<a href="https://consumer.ftc.gov/articles/job-scams">Beware of job scams</a>'
        '<a href="https://joinhandshake.com/employers/omicron">Students on Handshake</a>'), {}),
    # Pi: no careers link on the homepage, but the sitemap lists one.
    "https://pi.com/": (200, html("Pi"), {}),
    "https://pi.com/sitemap.xml": (200,
        '<?xml version="1.0"?><sitemapindex><sitemap><loc>https://pi.com/sitemap-news.xml</loc></sitemap>'
        '<sitemap><loc>https://pi.com/sitemap-pages.xml</loc></sitemap></sitemapindex>',
        {"content-type": "application/xml"}),
    "https://pi.com/sitemap-pages.xml": (200,
        '<urlset><url><loc>https://pi.com/en/about</loc></url>'
        '<url><loc>https://pi.com/en/about/careers-at-pi/benefits</loc></url>'
        '<url><loc>https://pi.com/en/about/careers-at-pi</loc></url></urlset>',
        {"content-type": "application/xml"}),
    "https://pi.com/en/about/careers-at-pi": (200, html("Careers at Pi", "<p>Open roles</p>"), {}),
    # JS-only homepage.
    "https://iota.com/": (200, "<html><body><div id=root></div></body></html>", {}),
}
KNOWN_HOSTS = {httpx.URL(u).host for u in SITE} | {"careers.acme.com", "jobs.acme.com"}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.url.host not in KNOWN_HOSTS:
        raise httpx.ConnectError("unknown host", request=request)
    if url in SITE:
        status, body, headers = SITE[url]
        return httpx.Response(status, text=body,
                              headers={"content-type": "text/html; charset=utf-8", **headers})
    return httpx.Response(404, text="not found", headers={"content-type": "text/html"})


def fetcher():
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(client=client)


COMPANIES = [
    ("Acme Corporation", "https://www.acme.com/"),
    ("Beta Inc", "https://beta.com/"),
    ("Gamma plc", "https://gamma.com/"),
    ("Delta AG", "https://delta.example/"),
    ("Epsilon SA", "https://epsilon.com/"),
    ("Zeta Ltd", None),
    ("Eta Group", "https://eta.io/"),
    ("Theta Holdings", "https://theta.com/"),
    ("Iota Co", "https://iota.com/"),
    ("Kappa Corp", "kappa.com"),  # no scheme, as some sources give it
    ("Lambda Logistics", "https://lambda.com/"),
    ("Mu Retail", "https://mu.com/"),
    ("Nu Post", "https://nupost.gov.in/"),
    ("Omicron SA", "https://omicron.com/"),
    ("Pi Industries", "https://pi.com/"),
]


@pytest.fixture
def loaded(conn):
    ingest(conn, "test", [
        CompanyRecord("test", str(i), name, 5000 - i, website=site)
        for i, (name, site) in enumerate(COMPANIES)
    ])
    return conn


def run(conn, **kw):
    async def go():
        f = fetcher()
        try:
            return await discover_all(conn, f, **kw)
        finally:
            await f.aclose()
    return asyncio.run(go())


def company(conn, name):
    return conn.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()


def portals(conn, name):
    return {r["career_page_url"]: r for r in conn.execute(
        "SELECT p.* FROM career_portals p JOIN companies c ON c.id = p.company_id"
        " WHERE c.name = ? ORDER BY p.id", (name,))}


def test_every_company_gets_an_outcome(loaded):
    summary = run(loaded)
    assert summary.selected == len(COMPANIES) == summary.accounted
    outcomes = {name: (company(loaded, name)["status"], company(loaded, name)["status_reason"])
                for name, _ in COMPANIES}
    assert outcomes == {
        "Acme Corporation": ("discovered", None),
        "Beta Inc": ("discovered", None),
        "Gamma plc": ("no_portal_found", "no_careers_link"),
        "Delta AG": ("failed", "site_unreachable"),
        "Epsilon SA": ("failed", "blocked_by_robots"),
        "Zeta Ltd": ("no_portal_found", "no_website"),
        "Eta Group": ("discovered", None),
        "Theta Holdings": ("needs_review", "careers_page_unreachable"),
        "Iota Co": ("no_portal_found", "no_links_in_html"),
        "Kappa Corp": ("discovered", None),
        "Lambda Logistics": ("discovered", None),
        "Mu Retail": ("discovered", None),
        "Nu Post": ("discovered", None),
        "Omicron SA": ("discovered", None),
        "Pi Industries": ("discovered", None),
    }
    assert build_report(loaded).ok
    events = loaded.execute(
        "SELECT COUNT(*) FROM pipeline_events WHERE stage = 'discover'").fetchone()[0]
    assert events == len(COMPANIES)


def test_finds_main_graduate_regional_and_ats_portals(loaded):
    run(loaded)
    found = portals(loaded, "Acme Corporation")
    assert list(found) == [
        "https://www.acme.com/careers",
        "https://www.acme.com/careers/graduates",
        "https://careers.acme.in/",
        "https://jobs.lever.co/acmelabs",
    ]
    main = found["https://www.acme.com/careers"]
    assert (main["scope"], main["ats_provider"]) == ("global", "Workday")
    assert main["final_ats_url"] == "https://acme.wd5.myworkdayjobs.com/en-US/External"
    assert main["confidence"] == 1.0
    grad = found["https://www.acme.com/careers/graduates"]
    assert (grad["scope"], grad["ats_provider"]) == ("graduate", "Greenhouse")
    india = found["https://careers.acme.in/"]
    assert (india["scope"], india["region"], india["ats_provider"]) == ("regional", "India", "Oracle/Taleo")
    assert found["https://jobs.lever.co/acmelabs"]["ats_provider"] == "Lever"
    assert "https://www.acme.com/careers/benefits" not in found


def test_probe_and_direct_ats_link(loaded):
    run(loaded)
    beta = portals(loaded, "Beta Inc")["https://beta.com/jobs"]
    assert beta["discovered_via"] == "path_probe"
    assert beta["ats_provider"] is None
    assert 0 < beta["confidence"] < 1
    eta = portals(loaded, "Eta Group")["https://jobs.lever.co/eta"]
    assert (eta["ats_provider"], eta["final_ats_url"]) == ("Lever", "https://jobs.lever.co/eta")


def test_unreachable_careers_page_is_kept_for_review(loaded):
    run(loaded)
    theta = portals(loaded, "Theta Holdings")["https://careers.theta.com/"]
    assert theta["http_status"] == 403
    assert json.loads(theta["evidence"])["fetch_error"] == "http_403"


def test_rerun_replaces_portals_and_retries_selected_statuses(loaded):
    run(loaded)
    before = loaded.execute("SELECT COUNT(*) FROM career_portals").fetchone()[0]
    summary = run(loaded, statuses=("discovered", "failed"))
    assert summary.selected == 11
    assert loaded.execute("SELECT COUNT(*) FROM career_portals").fetchone()[0] == before
    assert build_report(loaded).ok


def test_unexpected_error_is_recorded_not_lost(loaded, monkeypatch):
    original = disc.CompanyDiscovery.run

    async def boom(self):
        if self.company.name == "Beta Inc":
            raise RuntimeError("parser exploded")
        return await original(self)

    monkeypatch.setattr(disc.CompanyDiscovery, "run", boom)
    summary = run(loaded)
    assert summary.accounted == len(COMPANIES)
    beta = company(loaded, "Beta Inc")
    assert (beta["status"], beta["status_reason"]) == ("failed", "internal_error")


def test_export_has_one_row_per_portal(loaded, tmp_path):
    run(loaded)
    out = tmp_path / "out.csv"
    # Acme 4 + Lambda 3 + Beta, Eta, Theta, Kappa, Mu, Nu, Omicron, Pi 1 each
    # + 5 companies without portals
    assert export_csv(loaded, out) == 20


def test_link_score_prefers_main_careers_page():
    links = [
        Link("https://x.com/careers/graduates", "Graduate careers", "a"),
        Link("https://x.com/careers", "Careers", "a"),
        Link("https://x.com/uk/careers", "Careers UK", "a"),
    ]
    assert max(links, key=link_score).url == "https://x.com/careers"


@pytest.mark.parametrize("text,url,expected", [
    ("Students & graduates", "https://x.com/careers/students", ("graduate", None)),
    ("Careers in Germany", "https://x.com/de/careers", ("regional", "Germany")),
    ("Jobs", "https://x.com/careers/united-kingdom", ("regional", "United Kingdom")),
    ("Our brands' jobs", "https://brand.com/jobs", ("affiliate", None)),
    ("Jobs", "https://jobs.lever.co/x", ("other", None)),
])
def test_classify_scope(text, url, expected):
    assert classify_scope(text, url, {"x.com"}) == expected


def test_helpers():
    assert registrable_domain("https://careers.bbc.co.uk/x") == "bbc.co.uk"
    assert name_matches("Acme Corporation", "https://acme.wd5.myworkdayjobs.com/x")
    assert name_matches("General Motors Company", "https://generalmotors.wd5.myworkdayjobs.com")
    assert not name_matches("General Motors Company", "https://general-electric.example")
    assert region_of("US careers", "/") is None  # bare "US" is too ambiguous
    assert region_of("Careers in the USA", "/") == "USA"


def test_dead_host_is_not_probed_path_by_path(loaded):
    requested = []

    def counting(request):
        requested.append(str(request.url))
        return handler(request)

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(counting), follow_redirects=True)
        f = Fetcher(client=client, retries=0)
        try:
            delta = Company(0, "Delta AG", "https://delta.example/")
            return await disc.discover_company(f, delta)
        finally:
            await f.aclose()

    result = asyncio.run(go())
    assert (result.status, result.reason) == ("failed", "site_unreachable")
    assert not any(u.startswith("https://delta.example/careers") for u in requested)


def test_hosted_career_site_uses_page_not_script_as_final_url(loaded):
    run(loaded)
    found = portals(loaded, "Lambda Logistics")
    assert list(found) == [
        "https://careers.lambda.com/global/en/",
        "https://careers.lambda.com/global/en/students-graduates",
        "https://careers.lambda.com/global/en/lambda-usa",
    ]
    main = found["https://careers.lambda.com/global/en/"]
    assert (main["ats_provider"], main["final_ats_url"]) == ("Phenom", "https://careers.lambda.com/global/en/")
    assert json.loads(main["evidence"])["ats_found_via"] == "page_scripts"
    assert found["https://careers.lambda.com/global/en/lambda-usa"]["region"] == "USA"


def test_search_link_beats_sign_in_and_own_domain_pages_are_not_affiliates(loaded):
    run(loaded)
    found = portals(loaded, "Mu Retail")
    assert list(found) == ["https://mu.jobs/"]
    assert found["https://mu.jobs/"]["final_ats_url"] == "https://mu.wd1.myworkdayjobs.com/MuCareers"


def test_html_without_content_type_is_accepted():
    from portalfinder.web import Page
    assert Page("u", "u", 200, "<!DOCTYPE html><html></html>", "").ok
    assert not Page("u", "u", 200, '{"a": 1}', "application/json").ok
    assert not Page("u", "u", 404, "<html></html>", "text/html").ok


def test_government_company_keeps_its_own_site_but_drops_gov_links(loaded):
    run(loaded)
    assert list(portals(loaded, "Nu Post")) == ["https://nupost.gov.in/careers"]


def test_tracking_params_embedded_board_and_third_party_links(loaded):
    run(loaded)
    found = portals(loaded, "Omicron SA")
    assert list(found) == ["https://jobs.omicron.com/fr"]
    main = found["https://jobs.omicron.com/fr"]
    assert (main["ats_provider"], main["final_ats_url"]) == ("Greenhouse", "https://boards.greenhouse.io/omicron")


def test_sitemap_finds_careers_page_without_homepage_link(loaded):
    run(loaded)
    found = portals(loaded, "Pi Industries")
    assert list(found) == ["https://pi.com/en/about/careers-at-pi"]
    assert found["https://pi.com/en/about/careers-at-pi"]["discovered_via"] == "sitemap"


def test_find_ats_link_skips_sandbox_and_graduate_boards():
    from portalfinder.discover import ParsedPage, find_ats_link
    page = ParsedPage("Careers", [
        Link("https://att.wd1.myworkdayjobs.com/ATTcollege", "College jobs", "a"),
        Link("https://hp-sandbox.eightfold.ai/careers", "Search jobs", "a"),
        Link("https://att.wd1.myworkdayjobs.com/ATTGeneral", "Search jobs", "a"),
    ], "")
    assert find_ats_link(page) == "https://att.wd1.myworkdayjobs.com/ATTGeneral"


def test_third_party_sites_matched_by_domain_name():
    from portalfinder.discover import _excluded
    assert _excluded("https://uk.indeed.com/cmp/acme")
    assert _excluded("https://www.indeed.co.uk/jobs")
    assert _excluded("https://joinhandshake.com/x")
    assert _excluded("https://x.com/acme")
    assert not _excluded("https://www.xerox.com/en-us/jobs")
    assert not _excluded("https://www.dropbox.com/jobs")


def test_discover_all_redoes_finished_companies_but_not_excluded(tmp_path):
    from typer.testing import CliRunner
    from portalfinder.cli import app
    from portalfinder.db import connect
    db = tmp_path / "t.db"
    conn = connect(db)
    ingest(conn, "test", [CompanyRecord("test", "1", "Done Co", 5000),
                          CompanyRecord("test", "2", "Gone Co", 5000)])
    conn.execute("UPDATE companies SET status = 'discovered' WHERE name = 'Done Co'")
    conn.execute("UPDATE companies SET status = 'excluded', status_reason = 'dissolved'"
                 " WHERE name = 'Gone Co'")
    conn.commit()
    result = CliRunner().invoke(app, ["discover", "--db", str(db), "--all"])
    assert result.exit_code == 0, result.output
    assert "Processed 1 companies" in result.output
