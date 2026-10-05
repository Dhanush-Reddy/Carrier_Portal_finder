# Carrier Portal Finder

Builds a database of companies with more than 1,000 employees and finds their
official career portals: every portal a company runs (global, regional,
subsidiary, graduate, business-unit), the final ATS URL behind each one, and
which ATS provider it uses.

The core rule is that **no company is silently skipped**. Every source record
ends up inserted, updated, merged or rejected with a logged reason, and every
run is reconciled so the counts must add up.

## Status

| Stage | State |
|---|---|
| Wikidata ingest (company, employees, LinkedIn URL, website, country, industry, parent) | Done |
| Reconciliation report, CSV export | Done |
| ATS detection from a URL | Done (used by discovery) |
| People Data Labs free dataset ingest | Next |
| Career portal discovery (careers links, redirects, regional portals) | Planned |
| Verification and confidence scoring | Planned |

## Data sources

LinkedIn is not scraped; its User Agreement forbids automated collection.
LinkedIn company URLs come from sources that already carry them:

- **Wikidata** (CC0): employees (P1128), website (P856), country (P17),
  industry (P452), parent organisation (P749), LinkedIn company ID (P4264).
  Only items that are a subclass of *business* (Q4830453) are included.
- **People Data Labs free company dataset** (next): broad coverage of the
  1,001+ size bands, with LinkedIn URLs.

## Usage

```bash
pip install -e ".[dev]"

portalfinder ingest-wikidata --db portalfinder.db   # needs access to query.wikidata.org
portalfinder report --db portalfinder.db            # exits 1 if anything doesn't reconcile
portalfinder export --db portalfinder.db --out exports/companies.csv
pytest
```

## How "nothing skipped" works

- Wikidata ingest first lists every qualifying ID, then fetches details in
  batches. An ID that comes back without details still produces a record,
  which ingest rejects as `missing_name` instead of losing it.
- Records are matched by source ID (an update), then by LinkedIn URL (a merge
  across sources). A shared website domain is **not** merged, because
  subsidiaries often share their parent's domain. It is logged as
  `possible_duplicate` for review.
- `ingest_runs` stores seen/inserted/updated/merged/rejected counts, and the
  run fails if they don't add up. `portalfinder report` re-checks this, plus:
  every company has a source, every unresolved company has a reason, and
  every rejection has a logged event.
- The CSV export has one row per portal and one row for each company that
  has no portal yet, so every company appears.

## Data model (SQLite)

- `companies`: one row per company, with pipeline `status` and `status_reason`.
- `company_sources`: provenance, one row per source record.
- `company_relationships`: parent/subsidiary links. The parent is kept even
  if it is not in the dataset.
- `career_portals`: many per company, with `scope`, `region`,
  `final_ats_url`, `ats_provider`, `verification_status`, `confidence` and
  `last_verified_at`.
- `ingest_runs` and `pipeline_events`: the audit trail.
