import asyncio
import csv
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from portalfinder.sources import linkedin_mcp  # noqa: E402
from portalfinder.sources.linkedin_mcp import (  # noqa: E402
    ToolInfo, employee_band, parse_company, pick_argument, pick_tool)

FAKE = Path(__file__).with_name("fake_linkedin_mcp.py")
SERVER = {"command": sys.executable, "args": [str(FAKE)]}


async def no_sleep(_seconds):
    return None


def fetch(companies, tmp_path, **kw):
    return asyncio.run(linkedin_mcp.fetch_companies(
        SERVER, companies, tmp_path / "out.csv", tmp_path / "cache", delay=0,
        sleep=no_sleep, countries=["India", "Germany"], **kw))


def company(slug, name=None, country=""):
    return {"name": name or slug, "linkedin_url": f"https://www.linkedin.com/company/{slug}",
            "website": "", "country": country}


def test_fetch_parses_text_and_json_answers(tmp_path):
    summary, rows = fetch([company("wipro", "Wipro"), company("zoho", "Zoho"),
                           company("nobody")], tmp_path)
    assert (summary.ok, summary.not_found, summary.calls) == (2, 1, 3)
    wipro, zoho, nobody = rows
    assert wipro["industry"] == "IT Services and IT Consulting"
    assert (wipro["employee_band"], wipro["employee_count"]) == ("10,001+", 10001)
    assert wipro["website"] == "https://www.wipro.com"
    assert wipro["country"] == "India"  # from "Bengaluru, Karnataka"
    assert wipro["founded"] == "1945"
    assert zoho["industry"] == "Software Development"
    assert zoho["headquarters"] == "Chennai, Tamil Nadu" and zoho["country"] == "India"
    assert zoho["linkedin_url"] == "https://www.linkedin.com/company/zoho"
    assert nobody["status"] == "not_found"
    written = list(csv.DictReader((tmp_path / "out.csv").open()))
    assert [r["status"] for r in written] == ["ok", "ok", "not_found"]


def test_rerun_uses_cache_and_limit_leaves_rest_for_later(tmp_path):
    companies = [company("wipro"), company("zoho"), company("nobody")]
    summary, rows = fetch(companies, tmp_path, limit=1)
    assert (summary.calls, summary.ok, summary.not_fetched) == (1, 1, 2)
    assert rows[1]["status"] == "not_fetched"
    summary, rows = fetch(companies, tmp_path, limit=1)
    assert (summary.calls, summary.ok, summary.not_fetched) == (1, 2, 1)
    summary, rows = fetch(companies, tmp_path)
    assert (summary.calls, summary.ok, summary.not_found) == (1, 2, 1)
    summary, _ = fetch(companies, tmp_path)
    assert summary.calls == 0  # everything cached


def test_stops_when_linkedin_pushes_back(tmp_path):
    summary, rows = fetch([company("throttled"), company("wipro")], tmp_path)
    assert summary.stopped and "rate limit" in summary.stopped
    assert summary.calls == 1
    assert [r["status"] for r in rows] == ["error", "not_fetched"]
    assert not (tmp_path / "cache" / "throttled.json").exists()  # retried next run


def test_stickerdaniel_answer_shape(tmp_path):
    summary, rows = fetch([company("tcs", "TCS")], tmp_path)
    [tcs] = rows
    assert tcs["status"] == "ok"
    assert tcs["industry"] == "IT Services and IT Consulting"
    assert (tcs["employee_band"], tcs["employee_count"]) == ("10,001+", 10001)
    assert tcs["website"] == "http://www.tcs.com"
    assert tcs["headquarters"] == "Mumbai, Maharashtra" and tcs["country"] == "India"
    assert tcs["founded"] == "1968"
    assert tcs["linkedin_url"] == "https://www.linkedin.com/company/tata-consultancy-services"


def test_stops_on_rate_limit_inside_an_answer(tmp_path):
    summary, rows = fetch([company("soft-limited"), company("wipro")], tmp_path)
    assert summary.stopped and "rate limit" in summary.stopped
    assert [r["status"] for r in rows] == ["error", "not_fetched"]
    assert not (tmp_path / "cache" / "soft-limited.json").exists()


def test_pick_tool_and_argument():
    tools = [ToolInfo("search_jobs", "", {"keywords": {}}, ["keywords"]),
             ToolInfo("get_company_profile", "", {"company_name": {}, "get_employees": {}},
                      ["company_name"]),
             ToolInfo("search_companies", "", {"query": {}}, ["query"])]
    tool = pick_tool(tools)
    assert tool.name == "get_company_profile"
    assert pick_argument(tool) == "company_name"
    assert pick_argument(ToolInfo("t", "", {"linkedin_url": {}, "name": {}}, [])) == "linkedin_url"
    with pytest.raises(linkedin_mcp.McpSetupError):
        pick_tool(tools, "nope")
    with pytest.raises(linkedin_mcp.McpSetupError):
        pick_tool([tools[0]])


def test_argument_value():
    c = company("tata-elxsi", "Tata Elxsi")
    assert linkedin_mcp.argument_value("company_name", c) == "tata-elxsi"
    assert linkedin_mcp.argument_value("linkedin_url", c) == "https://www.linkedin.com/company/tata-elxsi"
    assert linkedin_mcp.argument_value("company_name", {"name": "Ola", "linkedin_url": ""}) == "Ola"


@pytest.mark.parametrize("text,expected", [
    ("10,001+ employees", ("10,001+", 10001)),
    ("1,001-5,000 employees", ("1,001-5,000", 1001)),
    ("5001 - 10000", ("5001-10000", 5001)),
    ("", (None, None)),
])
def test_employee_band(text, expected):
    assert employee_band(text) == expected


def test_parse_company_json_with_staff_count():
    details = parse_company(json.dumps({"company": {
        "name": "Acme", "staffCount": 2345, "industries": ["Software Development"]}}))
    assert details["employee_count"] == 2345
    assert details["industry"] == "Software Development"
    assert details["name"] == "Acme"


def test_load_server_from_claude_configs(tmp_path):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"mcpServers": {
        "linkedin-scraper": {"command": "uvx", "args": ["linkedin-mcp"], "env": {"X": "1"}},
        "files": {"command": "npx"}}}))
    assert linkedin_mcp.load_server(desktop)["command"] == "uvx"
    code = tmp_path / ".claude.json"
    code.write_text(json.dumps({"projects": {"C:/work": {"mcpServers": {
        "li": {"type": "http", "url": "http://localhost:8000/mcp"}}}}}))
    assert linkedin_mcp.load_server(code, "li")["url"].endswith("/mcp")
    with pytest.raises(linkedin_mcp.McpSetupError):
        linkedin_mcp.load_server(code, "other")
    jobhunt = tmp_path / "mcp-connections.json"  # job-hunt's data/mcp-connections.json
    jobhunt.write_text(json.dumps({"linkedin": {"transport": "stdio", "command": "python.exe",
                                                "args": ["-m", "linkedin_mcp_server"]},
                                   "naukri": {"transport": "stdio", "command": "python.exe"}}),
                       encoding="utf-8-sig")
    assert linkedin_mcp.load_server(jobhunt)["args"] == ["-m", "linkedin_mcp_server"]


def test_cli_fetch_and_ingest(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from portalfinder.cli import app
    from portalfinder.db import connect
    from portalfinder.export import export_csv
    monkeypatch.chdir(tmp_path)
    inp = tmp_path / "in.csv"
    inp.write_text("Company,LinkedIn\nWipro,https://www.linkedin.com/company/wipro/\n"
                   "Tiny,https://www.linkedin.com/company/tiny-shop\n")
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"linkedin": SERVER}}))
    runner = CliRunner()
    listed = runner.invoke(app, ["mcp-tools", "--mcp-config", str(config)])
    assert listed.exit_code == 0, listed.output
    assert "get_company_profile(company_name*, sections)" in listed.output
    result = runner.invoke(app, [
        "linkedin-fetch", "--mcp-config", str(config), "--input", str(inp),
        "--delay", "0", "--db", "t.db", "--ingest"])
    assert result.exit_code == 0, result.output
    assert "2 found" in result.output
    assert "inserted 1" in result.output and "rejected 1" in result.output  # 2-10 employees
    conn = connect(tmp_path / "t.db")
    assert export_csv(conn, tmp_path / "it.csv", sectors=["it"], per_company=True) == 1
    [row] = list(csv.DictReader((tmp_path / "it.csv").open()))
    assert (row["company_name"], row["country"], row["employee_count"]) == ("Wipro", "India", "10001")
    assert runner.invoke(app, ["linkedin-fetch"]).exit_code == 2  # no server given


def test_bundled_seed_list():
    from portalfinder.cli import _seed_companies
    seed = _seed_companies()
    assert len(seed) > 50
    assert {"Wipro", "HCLTech", "Zoho"} <= {c["name"] for c in seed}
    assert all(c["country"] for c in seed)
