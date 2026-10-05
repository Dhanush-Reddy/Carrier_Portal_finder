import csv
import gzip
import json
import zipfile

import pytest

from portalfinder.ingest import ingest
from portalfinder.records import CompanyRecord
from portalfinder.sources import pdl

FIELDS = ["id", "name", "website", "founded", "size", "locality", "region", "country",
          "industry", "linkedin_url"]
ROWS = [
    ["a1", "wipro", "wipro.com", "1945", "10001+", "bengaluru", "karnataka", "india",
     "information technology and services", "linkedin.com/company/wipro"],
    ["a2", "infosys", "infosys.com", "1981", "10001+", "bengaluru", "karnataka", "india",
     "information technology and services", "linkedin.com/company/infosys"],
    ["a3", "zoho corporation", "zoho.com", "1996", "5001-10000", "chennai", "tamil nadu", "india",
     "computer software", "linkedin.com/company/zoho"],
    ["a4", "tiny startup", "tiny.example", "2020", "11-50", "pune", "maharashtra", "india",
     "computer software", "linkedin.com/company/tiny"],
    ["a5", "acme corp", "acme.example", "1990", "1001-5000", "austin", "texas", "united states",
     "computer software", "linkedin.com/company/acme"],
    ["a6", "mid co", "mid.example", "2001", "501-1000", "delhi", "delhi", "india",
     "internet", "linkedin.com/company/midco"],
]


def write_csv(path, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(FIELDS)
        w.writerows(ROWS)
    return path


def write_jsonl(path):
    path.write_text("\n".join(json.dumps(dict(zip(FIELDS, r))) for r in ROWS) + "\n")
    return path


@pytest.mark.parametrize("make", ["csv", "pipe", "jsonl", "zip", "gz"])
def test_iter_rows_formats(tmp_path, make):
    if make == "csv":
        path = write_csv(tmp_path / "c.csv")
    elif make == "pipe":
        path = write_csv(tmp_path / "c.txt", "|")
    elif make == "jsonl":
        path = write_jsonl(tmp_path / "c.json")
    elif make == "zip":
        inner = write_csv(tmp_path / "free_company_dataset.csv")
        path = tmp_path / "c.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.write(inner, "free_company_dataset.csv")
    else:
        path = tmp_path / "c.json.gz"
        with gzip.open(path, "wt") as fh:
            fh.write(write_jsonl(tmp_path / "x.json").read_text())
    rows = list(pdl.iter_rows(path))
    assert len(rows) == len(ROWS)
    assert rows[0]["name"] == "wipro" and rows[0]["size"] == "10001+"


def test_records_keep_country_and_large_bands(tmp_path):
    path = write_csv(tmp_path / "c.csv")
    scan = pdl.ScanSummary()
    recs = list(pdl.records(path, ["India"], 1000, scan))
    assert [r.name for r in recs] == ["Wipro", "Infosys", "Zoho Corporation"]
    assert (scan.read, scan.kept) == (6, 3)
    wipro = recs[0]
    assert wipro.employee_count == 10001
    assert wipro.website == "https://wipro.com"
    assert wipro.linkedin_url == "https://www.linkedin.com/company/wipro"
    assert wipro.country == "India"
    assert wipro.industry == "information technology and services"
    assert recs[2].employee_count == 5001
    assert len(list(pdl.records(path, [], 1000))) == 4  # every country


@pytest.mark.parametrize("raw,expected", [
    ("tata consultancy services", "Tata Consultancy Services"),
    ("hcl technologies", "HCL Technologies"),
    ("bank of america", "Bank of America"),
    ("larsen & toubro infotech", "Larsen & Toubro Infotech"),
    ("l&t technology services", "L&T Technology Services"),
    ("IBM", "IBM"),  # already cased: kept
])
def test_nice_name(raw, expected):
    assert pdl.nice_name(raw) == expected


def test_band_lower_bound():
    assert pdl.band_lower_bound("1001-5000") == 1001
    assert pdl.band_lower_bound("10001+") == 10001
    assert pdl.band_lower_bound("") is None


def test_merge_on_domain_joins_one_existing_company(tmp_path):
    from portalfinder.db import connect
    conn = connect(tmp_path / "t.db")
    ingest(conn, "wikidata", [
        CompanyRecord("wikidata", "Q1", "Infosys limited", 200000, website="https://www.infosys.com",
                      country="India"),
        CompanyRecord("wikidata", "Q2", "Shared One", 5000, website="https://shared.example"),
        CompanyRecord("wikidata", "Q3", "Shared Two", 5000, website="https://shared.example"),
    ])
    run = ingest(conn, "pdl", [
        CompanyRecord("pdl", "a2", "Infosys", 10001, website="https://infosys.com",
                      linkedin_url="https://www.linkedin.com/company/infosys"),
        CompanyRecord("pdl", "s", "Shared", 1001, website="https://shared.example"),
    ], merge_on_domain=True)
    assert (run.merged, run.inserted) == (1, 1)  # two candidates for shared.example: no merge
    row = conn.execute("SELECT name, employee_count, linkedin_url FROM companies"
                       " WHERE website_domain = 'infosys.com'").fetchall()
    assert len(row) == 1
    assert tuple(row[0]) == ("Infosys limited", 200000, "https://www.linkedin.com/company/infosys")


def test_cli_ingest_pdl_then_export(tmp_path):
    from typer.testing import CliRunner
    from portalfinder.cli import app
    from portalfinder.db import connect
    from portalfinder.export import export_csv
    data = write_csv(tmp_path / "free_company_dataset.csv")
    db = tmp_path / "t.db"
    runner = CliRunner()
    result = runner.invoke(app, ["ingest-pdl", str(data), "--db", str(db), "--country", "india"])
    assert result.exit_code == 0, result.output
    assert "6 companies read; 3 match" in result.output
    assert "inserted 3" in result.output
    again = runner.invoke(app, ["ingest-pdl", str(data), "--db", str(db), "--country", "India"])
    assert "updated 3" in again.output
    out = tmp_path / "india.csv"
    assert export_csv(connect(db), out, countries=["India"], sectors=["it"], per_company=True) == 3
    names = [r["company_name"] for r in csv.DictReader(out.open())]
    assert names == ["Wipro", "Infosys", "Zoho Corporation"]
    missing = runner.invoke(app, ["ingest-pdl", str(tmp_path / "nope.csv"), "--db", str(db)])
    assert missing.exit_code == 2
