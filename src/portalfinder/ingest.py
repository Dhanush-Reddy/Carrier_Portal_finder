"""Load source records into the database without losing any of them.

Each record ends in exactly one of four outcomes, counted on the run:

- ``inserted``: a new company.
- ``updated``: the same source record seen before.
- ``merged``: a different source record for a company already present
  (matched on LinkedIn URL).
- ``rejected``: not stored as a company, with the reason in ``pipeline_events``.

Records that only share a website domain are *not* merged, since
subsidiaries often share their parent's domain. They are inserted and a
``possible_duplicate`` event is logged for review.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from portalfinder import MIN_EMPLOYEES
from portalfinder.db import log_event, now
from portalfinder.normalize import linkedin_company_url, website_domain
from portalfinder.records import CompanyRecord

FIELDS = ("name", "employee_count", "linkedin_url", "website", "country", "industry")

# More than any employer has (Walmart, the largest, has about 2.1 million).
# Counts above this are data errors in the source.
MAX_PLAUSIBLE_EMPLOYEES = 2_500_000


class ReconciliationError(RuntimeError):
    pass


@dataclass
class RunSummary:
    run_id: int
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    merged: int = 0
    rejected: int = 0

    @property
    def accounted(self) -> int:
        return self.inserted + self.updated + self.merged + self.rejected


def _event(conn, run_id, company_id, event, detail) -> None:
    log_event(conn, "ingest", event, detail, company_id=company_id, run_id=run_id)


def _reject_reason(rec: CompanyRecord, min_employees: int) -> str | None:
    if not rec.name or not rec.name.strip():
        return "missing_name"
    if rec.employee_count is None:
        return "missing_employee_count"
    if rec.employee_count <= min_employees:
        return "below_employee_threshold"
    if rec.employee_count > MAX_PLAUSIBLE_EMPLOYEES:
        return "implausible_employee_count"
    if rec.dissolved:
        return "dissolved"
    return None


def _record_values(rec: CompanyRecord) -> dict:
    return {
        "name": rec.name.strip(),
        "employee_count": rec.employee_count,
        "linkedin_url": linkedin_company_url(rec.linkedin_url),
        "website": rec.website,
        "country": rec.country,
        "industry": rec.industry,
    }


def _add_source(conn, company_id: int, rec: CompanyRecord) -> None:
    conn.execute(
        "INSERT INTO company_sources (company_id, source, source_id, raw, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (source, source_id) DO UPDATE SET raw = excluded.raw,"
        " fetched_at = excluded.fetched_at",
        (company_id, rec.source, rec.source_id, json.dumps(rec.raw, default=str), now()),
    )


def _add_parents(conn, company_id: int, rec: CompanyRecord) -> None:
    for parent in rec.parents:
        conn.execute(
            "INSERT INTO company_relationships"
            " (child_company_id, parent_source, parent_source_id, parent_name)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (child_company_id, parent_source, parent_source_id)"
            " DO UPDATE SET parent_name = excluded.parent_name",
            (company_id, rec.source, parent.source_id, parent.name),
        )


def _ingest_one(conn, run: RunSummary, rec: CompanyRecord, min_employees: int) -> None:
    reason = _reject_reason(rec, min_employees)
    if reason:
        run.rejected += 1
        existing = conn.execute(
            "SELECT company_id FROM company_sources WHERE source = ? AND source_id = ?",
            (rec.source, rec.source_id),
        ).fetchone()
        company_id = existing["company_id"] if existing else None
        if company_id is not None:
            # Loaded by an earlier run but no longer qualifies: keep the row so
            # it stays visible, but take it out of discovery.
            conn.execute(
                "UPDATE companies SET status = 'excluded', status_reason = ?, updated_at = ?"
                " WHERE id = ?", (reason, now(), company_id),
            )
        _event(conn, run.run_id, company_id, "rejected", {
            "reason": reason, "source": rec.source, "source_id": rec.source_id,
            "name": rec.name, "employee_count": rec.employee_count,
            "dissolved": rec.dissolved,
        })
        return

    values = _record_values(rec)
    values["website_domain"] = website_domain(rec.website)
    ts = now()

    row = conn.execute(
        "SELECT company_id FROM company_sources WHERE source = ? AND source_id = ?",
        (rec.source, rec.source_id),
    ).fetchone()
    if row:
        company_id = row["company_id"]
        sets = ", ".join(f"{k} = COALESCE(?, {k})" for k in values)
        conn.execute(
            f"UPDATE companies SET {sets}, updated_at = ? WHERE id = ?",
            (*values.values(), ts, company_id),
        )
        run.updated += 1
    else:
        match = None
        if values["linkedin_url"]:
            match = conn.execute(
                "SELECT id FROM companies WHERE linkedin_url = ?", (values["linkedin_url"],)
            ).fetchone()
        if match:
            company_id = match["id"]
            # Fill gaps only; keep the larger employee count.
            sets = ", ".join(
                f"{k} = COALESCE({k}, ?)" for k in values if k != "employee_count"
            )
            conn.execute(
                f"UPDATE companies SET {sets},"
                " employee_count = MAX(COALESCE(employee_count, 0), ?), updated_at = ?"
                " WHERE id = ?",
                (*(v for k, v in values.items() if k != "employee_count"),
                 values["employee_count"], ts, company_id),
            )
            run.merged += 1
            _event(conn, run.run_id, company_id, "merged", {
                "matched_on": "linkedin_url", "source": rec.source, "source_id": rec.source_id,
            })
        else:
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cur = conn.execute(
                f"INSERT INTO companies ({cols}, created_at, updated_at)"
                f" VALUES ({marks}, ?, ?)",
                (*values.values(), ts, ts),
            )
            company_id = cur.lastrowid
            run.inserted += 1
            if values["website_domain"]:
                others = conn.execute(
                    "SELECT id FROM companies WHERE website_domain = ? AND id != ?",
                    (values["website_domain"], company_id),
                ).fetchall()
                if others:
                    _event(conn, run.run_id, company_id, "possible_duplicate", {
                        "matched_on": "website_domain",
                        "domain": values["website_domain"],
                        "other_company_ids": [o["id"] for o in others],
                    })

    _add_source(conn, company_id, rec)
    _add_parents(conn, company_id, rec)


def link_parents(conn: sqlite3.Connection) -> None:
    """Point relationships at parent companies that are in the dataset."""
    conn.execute(
        "UPDATE company_relationships SET parent_company_id = ("
        " SELECT cs.company_id FROM company_sources cs"
        " WHERE cs.source = company_relationships.parent_source"
        " AND cs.source_id = company_relationships.parent_source_id)"
    )


def ingest(
    conn: sqlite3.Connection,
    source: str,
    records: Iterable[CompanyRecord],
    min_employees: int = MIN_EMPLOYEES,
) -> RunSummary:
    cur = conn.execute(
        "INSERT INTO ingest_runs (source, started_at) VALUES (?, ?)", (source, now())
    )
    run = RunSummary(run_id=cur.lastrowid)
    for rec in records:
        run.seen += 1
        _ingest_one(conn, run, rec, min_employees)
    link_parents(conn)
    conn.execute(
        "UPDATE ingest_runs SET finished_at = ?, records_seen = ?, records_inserted = ?,"
        " records_updated = ?, records_merged = ?, records_rejected = ? WHERE id = ?",
        (now(), run.seen, run.inserted, run.updated, run.merged, run.rejected, run.run_id),
    )
    conn.commit()
    if run.accounted != run.seen:
        raise ReconciliationError(
            f"run {run.run_id}: saw {run.seen} records but accounted for {run.accounted}"
        )
    return run
