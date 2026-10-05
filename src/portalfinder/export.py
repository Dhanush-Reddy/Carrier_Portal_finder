"""CSV export: one row per career portal.

Companies with no portal yet still get one row (with empty portal columns),
so the export always lists every company in the database (or every company
in the countries and organisation types asked for). Excluded companies are
listed without their automatically found portals.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

COLUMNS = [
    "company_id", "company_name", "employee_count", "linkedin_url", "website",
    "country", "industry", "organization_type", "parent_companies", "pipeline_status", "status_reason",
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


# Wikidata counts universities, hospitals and public bodies as businesses.
# They are kept, but labelled so they can be filtered out: by name, or by an
# industry that only they have ("higher education", not "education", which
# publishers and ed-tech companies also list). Anything else is a "company".
ORG_TYPES = (
    ("education", re.compile(
        r"universit|college|c[ée]gep|school|hochschule|polytechn|institute of technology",
        re.IGNORECASE), {"higher education", "university", "public university"}),
    ("healthcare", re.compile(
        r"hospitals?\b|clinic\b|klinik|krankenhaus|health (?:service|system|authority|board)|"
        r"medical cent(?:er|re)",
        re.IGNORECASE), {"hospital"}),
    ("public body", re.compile(
        r"ministry|^city of|county\b|armed forces|\barmy\b|council\b|^government",
        re.IGNORECASE), {"public administration"}),
)


def organization_type(name: str | None, industry: str | None) -> str:
    industries = {i.strip().lower() for i in (industry or "").split(";")}
    for label, pattern, only_theirs in ORG_TYPES:
        if pattern.search(name or "") or industries & only_theirs:
            return label
    return "company"


ORG_TYPE_NAMES = ("company",) + tuple(label for label, _, _ in ORG_TYPES)


def export_csv(
    conn: sqlite3.Connection,
    path: str | Path,
    countries: list[str] | None = None,
    org_types: list[str] | None = None,
) -> int:
    """Write the CSV; returns the number of rows.

    ``countries`` and ``org_types`` keep only companies with one of those
    values (country names as in the ``country`` column, any case).
    """
    wanted_countries = {c.strip().lower() for c in countries or []}
    wanted_types = {t.strip().lower() for t in org_types or []}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in conn.execute(QUERY):
            if wanted_countries and (row["country"] or "").lower() not in wanted_countries:
                continue
            org_type = organization_type(row["company_name"], row["industry"])
            if wanted_types and org_type not in wanted_types:
                continue
            out = {k: row[k] for k in COLUMNS if k != "organization_type"}
            out["organization_type"] = org_type
            writer.writerow(out)
            n += 1
    return n
