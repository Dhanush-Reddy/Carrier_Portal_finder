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
| Career portal discovery (careers links, ATS, graduate/regional/affiliate portals, confidence) | Done |
| Verification through ATS job APIs (Workday, Greenhouse, Lever, SmartRecruiters) | Next |
| People Data Labs free dataset ingest | Next |
| Headless-browser fallback for JavaScript-only sites, search-API fallback | Planned |

## Data sources

LinkedIn is not scraped; its User Agreement forbids automated collection.
LinkedIn company URLs come from sources that already carry them:

- **Wikidata** (CC0): employees (P1128), website (P856), country (P17),
  industry (P452), parent organisation (P749), LinkedIn company ID (P4264).
  Only items that are a subclass of *business* (Q4830453) are included.
- **People Data Labs free company dataset** (next): broad coverage of the
  1,001+ size bands, with LinkedIn URLs.

## Usage

Needs Python 3.11 or newer.

### Install

Windows (Command Prompt), from the repository folder:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Activate the virtual environment again in every new terminal. If the
`portalfinder` command is not found, use `python -m portalfinder` instead;
it takes the same arguments.

### Run

```
portalfinder ingest-wikidata --db portalfinder.db
portalfinder discover --db portalfinder.db --limit 50
portalfinder report --db portalfinder.db
portalfinder export --db portalfinder.db --out exports/companies.csv
```

1. `ingest-wikidata` loads the company list. It needs internet access to
   query.wikidata.org and takes a few minutes; progress is printed.
2. `discover` finds career portals, largest companies first. Leave out
   `--limit` to process every company. Add `--status failed` (or
   `--status no_portal_found`) to retry companies that ended in that status.
3. `report` prints counts per status and exits with an error if any company
   is unaccounted for.
4. `export` writes the CSV: one row per portal, plus one row for each
   company without a portal.

Run `portalfinder <command> --help` for every option.

### Tests

```
python -m pip install -e ".[dev]"
pytest
```

## How portal discovery works

For each company, largest first:

1. Fetch the official homepage and collect careers links (link text or URL
   with careers wording in about 20 languages, or a link straight into an ATS).
   LinkedIn, Indeed, Glassdoor and other third-party job boards are ignored.
2. With no careers link, try `/careers`, `/jobs`, `careers.<domain>`,
   `jobs.<domain>` and a few other common locations. A path that just
   redirects back to the homepage doesn't count.
3. The best-ranked link is the **global** portal. On it, find the link or
   embed into an ATS and follow redirects to get the **final ATS URL**.
4. Other careers links on the homepage or main careers page become extra
   portals when they are a **graduate** page (graduates, students, interns,
   apprentices...), a **regional** page (a country or region name), an
   **affiliate** site on another domain, or a different ATS tenant
   (**other**). Pages like `/careers/benefits` are not portals. Up to 6
   extras per company; any beyond that are counted in the event log.

Robots.txt is respected and requests identify themselves with a
`portalfinder/0.1` user agent.

**Confidence (0–1)** adds up these signals: linked from the official site
(0.30) or found by probing (0.20), on the company's own domain (0.20), ATS
detected (0.20), company name in the ATS URL or page title (0.15), and the
page shows jobs or links into an ATS (0.15). The signals are stored per
portal in `career_portals.evidence`.

**Company outcomes after discovery:**

| Status | Reason codes |
|---|---|
| `discovered` | at least one portal found |
| `needs_review` | `careers_page_unreachable`: careers link found but the page blocks or errors (often bot protection) |
| `no_portal_found` | `no_website`, `no_careers_link`, `no_links_in_html` (JavaScript-only site, needs the browser fallback) |
| `failed` | `site_unreachable`, `timeout`, `blocked_by_robots`, `http_<code>`, `internal_error` |

Re-running discovery for a company replaces its auto-discovered portals;
portals with `discovered_via = 'manual'` are kept.

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
- Discovery records exactly one outcome per company it selects, and
  raises if the count doesn't match. Unexpected errors become `failed` /
  `internal_error` with the traceback in `pipeline_events`.
- `report` also fails if a company is `discovered` but has no portal.
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
