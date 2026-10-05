import csv
import json

import pytest

from conftest import load_fixture
from portalfinder.export import export_csv
from portalfinder.ingest import ingest
from portalfinder.records import CompanyRecord
from portalfinder.report import build_report
from portalfinder.sources import wikidata


def wikidata_records():
    ids = {}
    wikidata.parse_list(load_fixture("wikidata_list.json"), ids)
    found = wikidata.parse_details(load_fixture("wikidata_details.json"), ids)
    return list(wikidata.complete_batch(sorted(ids), found, ids))


def test_every_record_is_accounted_for(conn):
    run = ingest(conn, "wikidata", wikidata_records())
    assert (run.seen, run.inserted, run.rejected) == (4, 3, 1)
    event = conn.execute("SELECT detail FROM pipeline_events WHERE event = 'rejected'").fetchone()
    assert json.loads(event["detail"])["reason"] == "missing_name"
    assert build_report(conn).ok


def test_rerun_updates_instead_of_duplicating(conn):
    ingest(conn, "wikidata", wikidata_records())
    run = ingest(conn, "wikidata", wikidata_records())
    assert (run.inserted, run.updated, run.rejected) == (0, 3, 1)
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 3
    assert build_report(conn).ok


def test_parent_is_linked_when_in_dataset(conn):
    ingest(conn, "wikidata", wikidata_records())
    row = conn.execute(
        "SELECT p.name FROM company_relationships r"
        " JOIN companies c ON c.id = r.child_company_id"
        " JOIN companies p ON p.id = r.parent_company_id WHERE c.name = 'Google'"
    ).fetchone()
    assert row["name"] == "Alphabet Inc."


def test_other_source_merges_on_linkedin_url(conn):
    ingest(conn, "wikidata", wikidata_records())
    run = ingest(conn, "pdl", [CompanyRecord(
        source="pdl", source_id="apple-1", name="Apple", employee_count=170000,
        linkedin_url="linkedin.com/company/Apple/",
    )])
    assert run.merged == 1
    row = conn.execute("SELECT name, employee_count FROM companies WHERE linkedin_url ="
                       " 'https://www.linkedin.com/company/apple'").fetchone()
    assert (row["name"], row["employee_count"]) == ("Apple Inc.", 170000)
    assert build_report(conn).ok


def test_shared_domain_is_flagged_not_merged(conn):
    ingest(conn, "wikidata", wikidata_records())
    run = ingest(conn, "pdl", [CompanyRecord(
        source="pdl", source_id="g-cloud", name="Google Cloud", employee_count=20000,
        website="https://google.com/cloud",
    )])
    assert run.inserted == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM pipeline_events WHERE event = 'possible_duplicate'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("count,reason", [
    (None, "missing_employee_count"), (1000, "below_employee_threshold"),
])
def test_rejections_are_logged_with_reason(conn, count, reason):
    run = ingest(conn, "pdl", [CompanyRecord("pdl", "x", "X Corp", count)])
    assert run.rejected == 1
    detail = conn.execute("SELECT detail FROM pipeline_events").fetchone()["detail"]
    assert json.loads(detail)["reason"] == reason


def test_report_flags_unresolved_company_without_reason(conn):
    ingest(conn, "wikidata", wikidata_records())
    conn.execute("UPDATE companies SET status = 'failed' WHERE name = 'Google'")
    rep = build_report(conn)
    assert not rep.ok
    assert "no reason" in rep.problems[0]


def test_export_lists_companies_without_portals(conn, tmp_path):
    ingest(conn, "wikidata", wikidata_records())
    google = conn.execute("SELECT id FROM companies WHERE name = 'Google'").fetchone()["id"]
    for url, scope in [("https://careers.google.com", "global"),
                       ("https://buildyourfuture.withgoogle.com", "graduate")]:
        conn.execute("INSERT INTO career_portals (company_id, career_page_url, scope)"
                     " VALUES (?, ?, ?)", (google, url, scope))
    out = tmp_path / "out.csv"
    assert export_csv(conn, out) == 4  # Google x2, Apple, Alphabet
    rows = list(csv.DictReader(out.open()))
    assert {r["company_name"] for r in rows} == {"Apple Inc.", "Google", "Alphabet Inc."}
    assert [r["parent_companies"] for r in rows if r["company_name"] == "Google"][0] == "Alphabet Inc."


@pytest.mark.parametrize("record,reason", [
    (CompanyRecord("wikidata", "Q1", "Cegep", 6_884_496), "implausible_employee_count"),
    (CompanyRecord("wikidata", "Q2", "Old Railways", 1_581_000, dissolved="1945-05-08"), "dissolved"),
])
def test_bad_source_data_is_rejected_with_reason(conn, record, reason):
    run = ingest(conn, "wikidata", [record])
    assert run.rejected == 1
    detail = json.loads(conn.execute("SELECT detail FROM pipeline_events").fetchone()["detail"])
    assert detail["reason"] == reason


def test_company_that_stops_qualifying_is_excluded_not_deleted(conn):
    ingest(conn, "wikidata", [CompanyRecord("wikidata", "Q9", "RAO UES", 577000)])
    run = ingest(conn, "wikidata", [CompanyRecord("wikidata", "Q9", "RAO UES", 577000,
                                                  dissolved="2008-07-01")])
    assert run.rejected == 1
    row = conn.execute("SELECT status, status_reason FROM companies").fetchone()
    assert (row["status"], row["status_reason"]) == ("excluded", "dissolved")
    assert build_report(conn).ok


def test_wikidata_dissolved_date_is_read():
    rows = [{"item": {"value": "http://www.wikidata.org/entity/Q5"},
             "itemLabel": {"value": "Deutsche Reichsbahn"},
             "dissolved": {"value": "1945-01-01T00:00:00Z"}}]
    rec = wikidata.parse_details(rows, {"Q5": 1581000})["Q5"]
    assert rec.dissolved == "1945-01-01"


def test_exclusion_removes_auto_discovered_portals(conn):
    ingest(conn, "wikidata", [CompanyRecord("wikidata", "Q9", "RAO UES", 577000)])
    cid = conn.execute("SELECT id FROM companies").fetchone()["id"]
    conn.execute("INSERT INTO career_portals (company_id, career_page_url, discovered_via)"
                 " VALUES (?, 'https://rao.ru/careers', 'homepage_link')", (cid,))
    conn.execute("INSERT INTO career_portals (company_id, career_page_url, discovered_via)"
                 " VALUES (?, 'https://rao.ru/manual', 'manual')", (cid,))
    ingest(conn, "wikidata", [CompanyRecord("wikidata", "Q9", "RAO UES", 577000, dissolved="2008-07-01")])
    left = [r["career_page_url"] for r in conn.execute("SELECT career_page_url FROM career_portals")]
    assert left == ["https://rao.ru/manual"]


def test_export_hides_stale_portals_of_excluded_companies(conn, tmp_path):
    # A database from before exclusion removed portals: Cegep was excluded
    # but still holds the portals found while it qualified.
    ingest(conn, "wikidata", [CompanyRecord("wikidata", "Q1", "Cegep", 5000)])
    cid = conn.execute("SELECT id FROM companies").fetchone()["id"]
    for url, via in [("https://cegep.ca/carrieres", "homepage_link"), ("https://cegep.ca/hand", "manual")]:
        conn.execute("INSERT INTO career_portals (company_id, career_page_url, discovered_via)"
                     " VALUES (?, ?, ?)", (cid, url, via))
    conn.execute("UPDATE companies SET status = 'excluded',"
                 " status_reason = 'implausible_employee_count'")
    out = tmp_path / "out.csv"
    assert export_csv(conn, out) == 1
    row = next(csv.DictReader(out.open()))
    assert (row["pipeline_status"], row["career_page_url"]) == ("excluded", "https://cegep.ca/hand")


@pytest.mark.parametrize("name,industry,expected", [
    ("University of Michigan", "higher education", "education"),
    ("Toronto District School Board", None, "education"),
    ("Oslo University Hospital", "hospital", "education"),  # name says university first
    ("Health Service Executive", None, "healthcare"),
    ("Ministry of National Defense", None, "public body"),
    ("Montgomery County Public Schools", None, "education"),
    ("Siemens", "industrial manufacturing; electrical industry", "company"),
    ("China Academy of Launch Vehicle Technology", "aerospace industry", "company"),
    ("Hilton Worldwide", "hospitality industry", "company"),
    ("Zakłady Chemiczne Police", "chemical industry", "company"),  # Police is a town
    ("Municipal Transport Company of Madrid", "public transport", "company"),
    ("Bertelsmann", "educational system; service; media", "company"),
    ("Massachusetts Institute of Technology", "higher education", "education"),
])
def test_organization_type(name, industry, expected):
    from portalfinder.export import organization_type
    assert organization_type(name, industry) == expected


def test_export_filters_by_country_and_type(tmp_path):
    from typer.testing import CliRunner
    from portalfinder.cli import app
    from portalfinder.db import connect
    db = tmp_path / "t.db"
    conn = connect(db)
    ingest(conn, "test", [
        CompanyRecord("test", "1", "Infosys", 300000, country="India"),
        CompanyRecord("test", "2", "University of Delhi", 5000, country="India", industry="higher education"),
        CompanyRecord("test", "3", "Siemens", 300000, country="Germany"),
    ])
    conn.commit()
    out = tmp_path / "in.csv"
    assert export_csv(conn, out, countries=["india"]) == 2
    rows = list(csv.DictReader(out.open()))
    assert {(r["company_name"], r["organization_type"]) for r in rows} == {
        ("Infosys", "company"), ("University of Delhi", "education")}
    assert export_csv(conn, out, countries=["India", "Germany"], org_types=["company"]) == 2
    runner = CliRunner()
    result = runner.invoke(app, ["export", "--db", str(db), "--out", str(out),
                                 "--country", "India", "--type", "company"])
    assert result.exit_code == 0, result.output
    assert "Wrote 1 rows" in result.output
    assert runner.invoke(app, ["export", "--db", str(db), "--type", "shop"]).exit_code == 2
    listed = runner.invoke(app, ["countries", "--db", str(db)])
    assert "India" in listed.output and "Germany" in listed.output
