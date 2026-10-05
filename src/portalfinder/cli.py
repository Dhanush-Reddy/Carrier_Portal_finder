"""Build a database of 1,000+ employee companies and their career portals."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from portalfinder import MIN_EMPLOYEES
from portalfinder.db import connect
from portalfinder.export import export_csv
from portalfinder.ingest import ingest
from portalfinder.report import build_report
from portalfinder.sources import wikidata

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
    ids = wikidata.list_qualifying_ids(client, min_employees)
    typer.echo(f"Wikidata lists {len(ids)} qualifying companies; fetching details...")
    records = wikidata.fetch_details(client, ids, batch_size)
    run = ingest(conn, wikidata.SOURCE, records, min_employees)
    if run.seen != len(ids):
        typer.echo(f"expected {len(ids)} records, ingested {run.seen}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"run {run.run_id}: seen {run.seen}, inserted {run.inserted}, updated {run.updated},"
        f" merged {run.merged}, rejected {run.rejected}"
    )


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
):
    """Write a CSV with one row per career portal (and one per company without any)."""
    n = export_csv(connect(db), out)
    typer.echo(f"Wrote {n} rows to {out}")


if __name__ == "__main__":
    app()
