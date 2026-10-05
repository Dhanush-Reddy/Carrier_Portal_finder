"""Reconciliation checks: prove that no company has gone missing."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from portalfinder.db import STATUSES


@dataclass
class Report:
    total_companies: int
    by_status: dict[str, int]
    runs: list[dict]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def build_report(conn: sqlite3.Connection) -> Report:
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM companies GROUP BY status")
    }
    runs = [dict(r) for r in conn.execute("SELECT * FROM ingest_runs ORDER BY id")]
    rep = Report(total, by_status, runs)

    if sum(by_status.values()) != total:
        rep.problems.append(f"status counts sum to {sum(by_status.values())}, not {total}")
    unknown = set(by_status) - set(STATUSES)
    if unknown:
        rep.problems.append(f"unknown statuses: {sorted(unknown)}")

    no_reason = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE status IN ('needs_review', 'no_portal_found', 'failed')"
        " AND (status_reason IS NULL OR status_reason = '')"
    ).fetchone()[0]
    if no_reason:
        rep.problems.append(f"{no_reason} companies are unresolved with no reason recorded")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM companies c WHERE NOT EXISTS"
        " (SELECT 1 FROM company_sources s WHERE s.company_id = c.id)"
    ).fetchone()[0]
    if orphans:
        rep.problems.append(f"{orphans} companies have no source record")

    portal_less = conn.execute(
        "SELECT COUNT(*) FROM companies c WHERE c.status IN ('discovered', 'verified')"
        " AND NOT EXISTS (SELECT 1 FROM career_portals p WHERE p.company_id = c.id)"
    ).fetchone()[0]
    if portal_less:
        rep.problems.append(f"{portal_less} companies are marked discovered/verified with no portal")

    for run in runs:
        accounted = (run["records_inserted"] + run["records_updated"]
                     + run["records_merged"] + run["records_rejected"])
        if run["finished_at"] is None:
            rep.problems.append(f"run {run['id']} ({run['source']}) did not finish")
        elif accounted != run["records_seen"]:
            rep.problems.append(
                f"run {run['id']} ({run['source']}): saw {run['records_seen']},"
                f" accounted for {accounted}"
            )
        rejected_logged = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE run_id = ? AND event = 'rejected'",
            (run["id"],),
        ).fetchone()[0]
        if rejected_logged != run["records_rejected"]:
            rep.problems.append(
                f"run {run['id']}: {run['records_rejected']} rejected but"
                f" {rejected_logged} rejection events logged"
            )
    return rep
