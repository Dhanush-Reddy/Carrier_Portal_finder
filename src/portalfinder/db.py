"""SQLite schema and connection helpers.

Every company ever ingested keeps a row with a pipeline status, and every
record that is rejected or merged is written to ``pipeline_events``, so the
totals of a run can always be reconciled.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Pipeline statuses a company can be in. Anything other than ``verified``
# must carry a ``status_reason`` once the company has been processed.
STATUSES = (
    "pending",
    "discovered",
    "verified",
    "needs_review",
    "no_portal_found",
    "failed",
    "excluded",  # no longer qualifies (e.g. dissolved); kept so it is visible
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    employee_count    INTEGER,
    linkedin_url      TEXT,
    website           TEXT,
    website_domain    TEXT,
    country           TEXT,
    industry          TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    status_reason     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_linkedin ON companies(linkedin_url);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(website_domain);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

-- One row per (source, source_id) so the same source record always maps
-- back to the same company, and provenance is kept per source.
CREATE TABLE IF NOT EXISTS company_sources (
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    raw         TEXT,
    fetched_at  TEXT NOT NULL,
    UNIQUE (source, source_id)
);

-- Parent/subsidiary links. The parent may not be in the dataset (it may be
-- under the size threshold), so its source id and name are kept as well.
CREATE TABLE IF NOT EXISTS company_relationships (
    child_company_id   INTEGER NOT NULL REFERENCES companies(id),
    parent_company_id  INTEGER REFERENCES companies(id),
    parent_source      TEXT NOT NULL,
    parent_source_id   TEXT NOT NULL,
    parent_name        TEXT,
    UNIQUE (child_company_id, parent_source, parent_source_id)
);

-- A company can have many portals: global, regional, subsidiary, graduate,
-- business-unit and so on.
CREATE TABLE IF NOT EXISTS career_portals (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    career_page_url   TEXT NOT NULL,
    final_ats_url     TEXT,
    ats_provider      TEXT,
    scope             TEXT NOT NULL DEFAULT 'global',
    region            TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    confidence        REAL,
    last_verified_at  TEXT,
    discovered_via    TEXT,
    page_title        TEXT,
    http_status       INTEGER,
    evidence          TEXT,
    UNIQUE (company_id, career_page_url)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    records_seen      INTEGER NOT NULL DEFAULT 0,
    records_inserted  INTEGER NOT NULL DEFAULT 0,
    records_updated   INTEGER NOT NULL DEFAULT 0,
    records_merged    INTEGER NOT NULL DEFAULT 0,
    records_rejected  INTEGER NOT NULL DEFAULT 0
);

-- Audit log. Rejected source records have no company_id but are still here.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES ingest_runs(id),
    company_id  INTEGER REFERENCES companies(id),
    stage       TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


# Columns added after a table was first created: (table, column, type).
MIGRATIONS = (
    ("career_portals", "page_title", "TEXT"),
    ("career_portals", "http_status", "INTEGER"),
    ("career_portals", "evidence", "TEXT"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def log_event(
    conn: sqlite3.Connection,
    stage: str,
    event: str,
    detail: dict,
    company_id: int | None = None,
    run_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pipeline_events (run_id, company_id, stage, event, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, company_id, stage, event, json.dumps(detail, default=str), now()),
    )


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
