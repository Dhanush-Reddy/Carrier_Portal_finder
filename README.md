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
| LinkedIn company details through your own LinkedIn MCP server | Done |
| People Data Labs free company dataset ingest | Done |
| Headless-browser fallback for JavaScript-only sites, search-API fallback | Planned |

## Data sources

LinkedIn is not scraped; its User Agreement forbids automated collection.
LinkedIn company URLs come from sources that already carry them:

- **Wikidata** (CC0): employees (P1128), website (P856), country (P17),
  industry (P452), parent organisation (P749), LinkedIn company ID (P4264).
  Only items that are a subclass of *business* (Q4830453) are included.
- **People Data Labs free company dataset**: about 22 million companies with
  size band, industry, country, website and LinkedIn URL. Broad coverage of
  the 1,001+ size bands, including the many Indian companies Wikidata lacks.

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
2. `discover` finds career portals for companies not processed yet,
   largest first. Leave out `--limit` to process every company. Add
   `--status failed` (or `--status no_portal_found`) to retry companies that
   ended in that status, or `--all` to redo every company that isn't
   excluded (useful after updating the tool; combine with `--limit`).
   `--country India` (repeatable) processes only companies in that country.
3. `report` prints counts per status and exits with an error if any company
   is unaccounted for.
4. `export` writes the CSV: one row per portal, plus one row for each
   company without a portal. Its `organization_type` column says whether
   the row is a `company` or an `education`, `healthcare` or `public body`
   organisation (Wikidata lists universities, hospitals and public bodies
   as businesses too). Filter the export with `--country` and `--type`,
   both repeatable, for example:

   ```
   portalfinder export --db portalfinder.db --out exports/india.csv --country India --type company
   ```

   Country names are the ones in the `country` column (any case);
   `portalfinder countries --db portalfinder.db` lists them with counts.

   The `sector` column is `it` for IT companies (software, IT services and
   consulting, internet, cloud, cybersecurity, computer hardware,
   semiconductors, video games), judged from the industry. `--sector it`
   keeps only those, and `--per-company` writes one row per company: its
   main careers page and ATS, with its other careers pages in one column.
   For a list of IT companies to load into another tool:

   ```
   portalfinder export --db portalfinder.db --out exports/it_companies.csv --sector it --type company --per-company
   ```

Run `portalfinder <command> --help` for every option.

### Company list from People Data Labs

For every company in a country with more than 1,000 employees, download the
free company dataset from <https://www.peopledatalabs.com/company-dataset>
(CSV, pipe-delimited or JSON; a zip file is fine, or unzip it with 7-Zip),
then:

```bat
portalfinder ingest-pdl path\to\free_company_dataset.zip --db portalfinder.db --country India
portalfinder discover --db portalfinder.db --country India
portalfinder export --db portalfinder.db --out exports\india_companies.csv --country India --type company --per-company
```

- `ingest-pdl` reads the whole file (a few minutes) and keeps the companies
  in the `--country` given (repeatable; all countries without it) whose
  size band starts above 1,000. The employee count is the band's lower
  bound: 1001-5000 becomes 1001, 10001+ becomes 10001.
- A company already loaded from Wikidata is updated rather than duplicated
  when the LinkedIn URL matches, or when it is the only company with the
  same website and has no other LinkedIn URL.
- `discover` then finds each company's career portals, and the export
  writes one row per company with its main careers page, ATS and other
  career pages. Add `--sector it` for IT companies only.

### Adding companies through your LinkedIn MCP server

Wikidata misses many large IT companies (Wipro, HCLTech, Zoho...). If you
have a LinkedIn MCP server set up in Claude Desktop or Claude Code,
`linkedin-fetch` starts it the same way, looks each company up, and writes
`exports/linkedin_companies.csv` (name, LinkedIn URL, website, industry,
employee band, headquarters, country, founded). It runs on your computer,
as your LinkedIn account.

```bat
python -m pip install -e ".[linkedin]"
portalfinder mcp-tools --mcp-config "%APPDATA%\Claude\claude_desktop_config.json"
portalfinder linkedin-fetch --mcp-config "%APPDATA%\Claude\claude_desktop_config.json" --db portalfinder.db --ingest
portalfinder discover --db portalfinder.db
portalfinder export --db portalfinder.db --out exports/it_companies.csv --sector it --type company --per-company
```

- The config is Claude Desktop's `claude_desktop_config.json` (on Windows in
  `%APPDATA%\Claude`), or Claude Code's `%USERPROFILE%\.claude.json` or a
  project's `.mcp.json`, or job-hunt's `data\mcp-connections.json` (which
  reuses job-hunt's signed-in LinkedIn session). The server whose name contains "linkedin" is used;
  pick another with `--server`, or give the command that starts it with
  `--mcp-command`.
- `mcp-tools` lists the server's tools. The company-profile tool and its
  input are found automatically; if not, name them with `--tool` and `--arg`.
- Without `--input`, the bundled list of about 65 large IT companies that
  Wikidata misses is looked up. `--input my_companies.csv` (columns `name`,
  `linkedin_url`, optional `website`, `country`) looks up your own list.
  `--from-db --missing-industry` looks up the database's companies whose
  industry is unknown, so the IT filter can see them.
- `--ingest` adds the companies to the database (matched with Wikidata ones
  by LinkedIn URL). Employee counts are the lower end of LinkedIn's band
  (10,001+ becomes 10001). Companies under 1,000 employees are rejected
  with a logged reason.
- LinkedIn restricts accounts that read many pages quickly, so calls are
  about 20 seconds apart (`--delay`), a run makes at most 150 calls
  (`--limit`), and it stops at once if the server reports a rate limit,
  a challenge or a sign-in problem. Answers are cached in `linkedin_cache`;
  run the same command again later to continue where it stopped.

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
2. With no careers link, look for careers pages in the site's sitemap
   (declared in robots.txt, or `/sitemap.xml`), then try `/careers`, `/jobs`,
   `careers.<domain>`, `jobs.<domain>` and other common locations. A path
   that just redirects back to the homepage doesn't count, and neither do
   news articles that happen to mention jobs.
3. The best-ranked link is the **global** portal. On it, find the link or
   embed into an ATS and follow redirects to get the **final ATS URL**,
   reduced to the portal's entry point (a Workday sign-in redirect becomes
   the career site, a Lever job advert becomes the company's board). Job
   search links are preferred over sign-in or profile links, and boards named
   after the company over others. Test (sandbox) boards are never used. A
   group page that only links its subsidiaries' boards gets no board of its
   own, so a subsidiary's board is not reported as the group's. A hosted career
   site on the company's own domain (common with Phenom, SuccessFactors and
   Avature) is its own final URL; its provider is read from the scripts it
   loads.
4. Other careers links on the homepage or main careers page become extra
   portals when they are a **graduate** page (graduates, students, interns,
   apprentices...), a **regional** page (a country or region name), an
   **affiliate** site on another domain, or a different ATS tenant
   (**other**). Pages like `/careers/benefits`, sign-in and account pages,
   single job adverts, PDFs, recruitment-fraud warnings, blog and news posts,
   employee stories, equal-opportunity statements, career advice,
   government sites and third-party boards (LinkedIn, Indeed, Handshake...)
   are not portals, and sub-pages of a portal already kept are skipped. So
   are variants of a page already kept: the same page with another
   `?locale=`, or a link that redirects to a page already kept.
   Tracking parameters (`utm_*`, `_ga`, `gclid`...) are removed from URLs.
   Per company: up to 30 regional, 5 affiliate, 3 graduate and 3 other
   extras; any beyond that, and the duplicates, are counted in the event log.

Robots.txt is respected as [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309)
says: its rules apply when it can be read; a 4xx answer (often a bot
firewall's 403) means there are no rules; a 5xx answer means nothing may be
fetched. Requests identify themselves with a `portalfinder/0.1` user agent,
and a "too many requests" (429) answer is retried once after the wait the
site asks for (up to 30 seconds).

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
| `failed` | `site_unreachable`, `timeout`, `blocked_by_robots`, `robots_unreachable` (robots.txt answers with a server error), `http_<code>`, `internal_error` |
| `excluded` | set at ingest: `dissolved`, `implausible_employee_count` (over 2.5 million, a source data error). Auto-discovered portals are removed (and left out of the export); the company row stays. |

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
