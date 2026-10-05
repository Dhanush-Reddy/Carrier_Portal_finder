"""CSV export: one row per career portal.

Companies with no portal yet still get one row (with empty portal columns),
so the export always lists every company in the database. Excluded companies
are listed without their automatically found portals.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

COLUMNS = [
    "company_id", "company_name", "employee_count", "linkedin_url", "website",
    "country", "industry", "parent_companies", "pipeline_status", "status_reason",
    "career_page_url", "portal_scope", "portal_region", "final_ats_url",
    "ats_provider", "verification_status", "confidence", "last_verified_at",
    "discovered_via",
]

QUERY = """
SELECT c.id AS company_id, c.name AS company_name, c.employee_count, c.linkedin_url,
       c.website, c.country, c.industry,
       (SELECT GROUP_CONCAT(COALESCE(p.name, r.parent_name, r.parent_source_id), '; ')
          FROM company_relationships r LEFT JOIN companies p ON p.id = r.parent_company_id
         WHERE r.child_company_id = c.id) AS parent_companies,
       c.status AS pipeline_status, c.status_reason,
       cp.career_page_url, cp.scope AS portal_scope, cp.region AS portal_region,
       cp.final_ats_url, cp.ats_provider, cp.verification_status, cp.confidence,
       cp.last_verified_at, cp.discovered_via
  FROM companies c
  -- An excluded company keeps only portals added by hand. (Databases from
  -- before ingest removed the others may still hold them.)
  LEFT JOIN career_portals cp ON cp.company_id = c.id
       AND (c.status != 'excluded' OR cp.discovered_via = 'manual')
 ORDER BY c.employee_count DESC, c.id, cp.id
"""


def export_csv(conn: sqlite3.Connection, path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in conn.execute(QUERY):
            writer.writerow({k: row[k] for k in COLUMNS})
            n += 1
    return n
