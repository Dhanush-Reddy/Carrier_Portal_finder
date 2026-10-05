"""Build a database of 1,000+ employee companies and their career portals."""

from __future__ import annotations

import asyncio
import json
from importlib.resources import as_file, files
from pathlib import Path

import typer

from portalfinder import MIN_EMPLOYEES
from portalfinder.db import STATUSES, connect
from portalfinder.discover import discover_all
from portalfinder.export import ORG_TYPE_NAMES, SECTOR_NAMES, export_csv, sector
from portalfinder.ingest import ingest
from portalfinder.report import build_report
from portalfinder.sources import linkedin_mcp, wikidata
from portalfinder.web import Fetcher

app = typer.Typer(no_args_is_help=True, help=__doc__)
DB_OPTION = typer.Option("portalfinder.db", "--db", help="SQLite database path.")


@app.command("ingest-wikidata")
def ingest_wikidata(
    db: Path = DB_OPTION,
    min_employees: int = typer.Option(MIN_EMPLOYEES, help="Exclusive lower bound."),
    batch_size: int = typer.Option(200, help="IDs per details query."),
):
    """Load every Wikidata business above the employee threshold."""
    conn = connect(db)
    client = wikidata.WikidataClient()
    ids = wikidata.list_qualifying_ids(
        client, min_employees,
        on_band=lambda lo, hi, n: typer.echo(
            f"  employees {lo:,}-{hi:,}: {n} companies" if hi else f"  employees >{lo:,}: {n} companies"),
    )
    typer.echo(f"Wikidata lists {len(ids)} qualifying companies; fetching details...")

    def with_progress(records):
        for i, rec in enumerate(records, 1):
            if i % 1000 == 0:
                typer.echo(f"  {i}/{len(ids)} fetched")
            yield rec

    records = with_progress(wikidata.fetch_details(client, ids, batch_size))
    run = ingest(conn, wikidata.SOURCE, records, min_employees)
    if run.seen != len(ids):
        typer.echo(f"expected {len(ids)} records, ingested {run.seen}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"run {run.run_id}: seen {run.seen}, inserted {run.inserted}, updated {run.updated},"
        f" merged {run.merged}, rejected {run.rejected}"
    )


@app.command()
def discover(
    db: Path = DB_OPTION,
    status: list[str] = typer.Option(
        ["pending"], help="Process companies in this status (repeatable), e.g. --status failed."
    ),
    all_: bool = typer.Option(
        False, "--all",
        help="Process every company that isn't excluded, including ones already done.",
    ),
    limit: int = typer.Option(None, help="Process at most this many companies, largest first."),
    company_id: list[int] = typer.Option(None, "--company-id", help="Only these company IDs."),
    country: list[str] = typer.Option(
        None, "--country", help='Only companies in this country (repeatable), e.g. --country India.'),
    concurrency: int = typer.Option(10, help="Companies fetched in parallel."),
):
    """Find career portals and their ATS for each company."""
    if all_:
        status = [s for s in STATUSES if s != "excluded"]
    bad = set(status) - set(STATUSES)
    if bad:
        typer.echo(f"unknown status: {', '.join(sorted(bad))}", err=True)
        raise typer.Exit(2)
    conn = connect(db)

    async def go():
        fetcher = Fetcher()
        try:
            return await discover_all(
                conn, fetcher, tuple(status), limit, concurrency, company_id or None,
                countries=country or None,
                on_result=lambda r: typer.echo(
                    f"  company {r.company_id}: {r.status}"
                    + (f" ({r.reason})" if r.reason else f", {len(r.portals)} portal(s)")
                ),
            )
        finally:
            await fetcher.aclose()

    summary = asyncio.run(go())
    typer.echo(f"Processed {summary.selected} companies: " + ", ".join(
        f"{k} {v}" for k, v in sorted(summary.by_status.items())) if summary.selected
        else "No companies matched.")


@app.command()
def report(db: Path = DB_OPTION, as_json: bool = typer.Option(False, "--json")):
    """Show status counts and check that no company is unaccounted for."""
    rep = build_report(connect(db))
    if as_json:
        typer.echo(json.dumps({
            "total_companies": rep.total_companies, "by_status": rep.by_status,
            "runs": rep.runs, "problems": rep.problems,
        }, indent=2))
    else:
        typer.echo(f"Companies: {rep.total_companies}")
        for status, n in sorted(rep.by_status.items()):
            typer.echo(f"  {status:<16} {n}")
        for run in rep.runs:
            typer.echo(
                f"Run {run['id']} {run['source']}: seen {run['records_seen']},"
                f" inserted {run['records_inserted']}, updated {run['records_updated']},"
                f" merged {run['records_merged']}, rejected {run['records_rejected']}"
            )
        typer.echo("Reconciliation: OK" if rep.ok else "Reconciliation FAILED:")
        for p in rep.problems:
            typer.echo(f"  - {p}")
    if not rep.ok:
        raise typer.Exit(1)


@app.command()
def export(
    db: Path = DB_OPTION,
    out: Path = typer.Option(Path("exports/companies.csv"), "--out"),
    country: list[str] = typer.Option(
        None, "--country", help='Only companies in this country (repeatable), e.g. --country "United States".'),
    org_type: list[str] = typer.Option(
        None, "--type",
        help=f"Only this organisation type (repeatable): {', '.join(ORG_TYPE_NAMES)}."),
    sector: list[str] = typer.Option(
        None, "--sector", help=f"Only this sector (repeatable): {', '.join(SECTOR_NAMES)}."),
    per_company: bool = typer.Option(
        False, "--per-company",
        help="One row per company (main portal plus a list of the others) instead of one per portal."),
):
    """Write a CSV with one row per career portal (and one per company without any)."""
    bad = {t.lower() for t in org_type or []} - set(ORG_TYPE_NAMES)
    if bad:
        typer.echo(f"unknown type: {', '.join(sorted(bad))}; use {', '.join(ORG_TYPE_NAMES)}", err=True)
        raise typer.Exit(2)
    bad = {s.lower() for s in sector or []} - set(SECTOR_NAMES)
    if bad:
        typer.echo(f"unknown sector: {', '.join(sorted(bad))}; use {', '.join(SECTOR_NAMES)}", err=True)
        raise typer.Exit(2)
    n = export_csv(connect(db), out, countries=country or None, org_types=org_type or None,
                   sectors=sector or None, per_company=per_company)
    typer.echo(f"Wrote {n} rows to {out}")
    if country and n == 0:
        typer.echo("No companies matched; see `portalfinder countries` for the names used.", err=True)


@app.command()
def countries(db: Path = DB_OPTION):
    """List countries with their number of companies (excluded ones left out)."""
    rows = connect(db).execute(
        "SELECT COALESCE(country, '(unknown)') AS c, COUNT(*) AS n FROM companies"
        " WHERE status != 'excluded' GROUP BY c ORDER BY n DESC, c")
    for r in rows:
        typer.echo(f"{r['n']:>6}  {r['c']}")


MCP_CONFIG_OPTION = typer.Option(
    None, "--mcp-config",
    help="MCP config file that lists the server: Claude Desktop's claude_desktop_config.json,"
         " Claude Code's .claude.json or a .mcp.json.")
SERVER_OPTION = typer.Option(
    None, "--server", help='Server name in that config (default: the one with "linkedin" in its name).')
MCP_COMMAND_OPTION = typer.Option(
    None, "--mcp-command", help='Or the command that starts the server, e.g. "uvx linkedin-mcp-server".')


def _mcp_server(mcp_config: Path | None, server: str | None, mcp_command: str | None) -> dict:
    try:
        if mcp_command:
            return linkedin_mcp.server_from_command(mcp_command)
        if mcp_config:
            return linkedin_mcp.load_server(mcp_config, server)
    except (linkedin_mcp.McpSetupError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    typer.echo("give --mcp-config (with --server) or --mcp-command", err=True)
    raise typer.Exit(2)


@app.command("mcp-tools")
def mcp_tools(
    mcp_config: Path = MCP_CONFIG_OPTION,
    server: str = SERVER_OPTION,
    mcp_command: str = MCP_COMMAND_OPTION,
):
    """List the tools an MCP server offers and the inputs each one takes."""
    spec = _mcp_server(mcp_config, server, mcp_command)
    try:
        tools = asyncio.run(linkedin_mcp.list_tools(spec))
    except linkedin_mcp.McpSetupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    for tool in tools:
        inputs = ", ".join(
            f"{name}{'*' if name in tool.required else ''}" for name in tool.properties)
        typer.echo(f"{tool.name}({inputs})")
        if tool.description:
            typer.echo(f"    {tool.description.strip().splitlines()[0][:150]}")


def _seed_companies() -> list[dict]:
    path = files("portalfinder").joinpath("data/it_companies_seed.csv")
    with as_file(path) as p:
        return linkedin_mcp.read_companies(p)


@app.command("linkedin-fetch")
def linkedin_fetch(
    mcp_config: Path = MCP_CONFIG_OPTION,
    server: str = SERVER_OPTION,
    mcp_command: str = MCP_COMMAND_OPTION,
    input_: Path = typer.Option(
        None, "--input",
        help="CSV of companies to look up (columns name, linkedin_url, website, country)."
             " Default: the bundled list of large IT companies that Wikidata misses."),
    from_db: bool = typer.Option(
        False, "--from-db", help="Look up the database's companies that have a LinkedIn URL instead."),
    country: list[str] = typer.Option(None, "--country", help="With --from-db: only this country (repeatable)."),
    sector_: list[str] = typer.Option(None, "--sector", help="With --from-db: only this sector, e.g. it."),
    missing_industry: bool = typer.Option(
        False, "--missing-industry",
        help="With --from-db: only companies whose industry is unknown (so the IT filter can't see them)."),
    tool: str = typer.Option(None, "--tool", help="The company-profile tool (see mcp-tools)."),
    arg: str = typer.Option(None, "--arg", help="The tool input that takes the company."),
    out: Path = typer.Option(Path("exports/linkedin_companies.csv"), "--out"),
    cache: Path = typer.Option(Path("linkedin_cache"), "--cache", help="Answers are kept here, so a rerun resumes."),
    delay: float = typer.Option(20.0, help="Seconds between calls (randomised by +/-30%)."),
    limit: int = typer.Option(150, help="Most calls in one run; cached companies don't count."),
    db: Path = DB_OPTION,
    ingest_: bool = typer.Option(
        False, "--ingest", help="Also add the companies found to the database (source: linkedin)."),
):
    """Look companies up through your LinkedIn MCP server and write a CSV."""
    spec = _mcp_server(mcp_config, server, mcp_command)
    known_countries: list[str] = []
    if from_db or ingest_ or db.exists():
        conn = connect(db)
        known_countries = [r[0] for r in conn.execute(
            "SELECT DISTINCT country FROM companies WHERE country IS NOT NULL")]
    if from_db:
        wanted_c = {c.lower() for c in country or []}
        wanted_s = {s.lower() for s in sector_ or []}
        companies = [
            {"name": r["name"], "linkedin_url": r["linkedin_url"], "website": r["website"] or "",
             "country": r["country"] or ""}
            for r in conn.execute(
                "SELECT name, linkedin_url, website, country, industry FROM companies"
                " WHERE linkedin_url IS NOT NULL AND status != 'excluded' ORDER BY employee_count DESC")
            if (not wanted_c or (r["country"] or "").lower() in wanted_c)
            and (not wanted_s or sector(r["industry"]) in wanted_s)
            and (not missing_industry or not r["industry"])
        ]
    elif input_:
        companies = linkedin_mcp.read_companies(input_)
    else:
        companies = _seed_companies()
    typer.echo(f"{len(companies)} companies to look up; up to {limit} calls, ~{delay:.0f}s apart")
    try:
        summary, rows = asyncio.run(linkedin_mcp.fetch_companies(
            spec, companies, out, cache, tool_name=tool, arg_name=arg, delay=delay,
            limit=limit, countries=known_countries, on_progress=typer.echo))
    except linkedin_mcp.McpSetupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    typer.echo(
        f"Wrote {len(rows)} rows to {out}: {summary.ok} found, {summary.not_found} not found,"
        f" {summary.errors} errors, {summary.not_fetched} not fetched yet ({summary.calls} calls)")
    if summary.stopped:
        typer.echo(f"{summary.stopped}. Wait before running again; finished companies are cached.", err=True)
    elif summary.not_fetched:
        typer.echo("Run the same command again later to continue.")
    if ingest_:
        run = ingest(conn, linkedin_mcp.SOURCE, linkedin_mcp.to_records(rows))
        conn.commit()
        typer.echo(f"Database: inserted {run.inserted}, merged {run.merged}, updated {run.updated},"
                   f" rejected {run.rejected} (rejections are listed in `report`)")


if __name__ == "__main__":
    app()
