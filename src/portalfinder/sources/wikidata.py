"""Wikidata source (CC0).

Two steps, so that every qualifying company is accounted for:

1. ``list_qualifying_ids`` asks for the IDs of every business with more than
   ``MIN_EMPLOYEES`` employees (the maximum reported value is used). The
   query is split into employee-count bands so no single query hits the
   public endpoint's 60-second limit.
2. ``fetch_details`` fetches the details for those IDs in small batches.

Any ID from step 1 that comes back without details is still returned as a
record (with ``name=None``), so ingest rejects it with a reason instead of it
disappearing.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator

import httpx

from portalfinder import MIN_EMPLOYEES
from portalfinder.normalize import linkedin_company_url
from portalfinder.records import CompanyRecord, Parent

SOURCE = "wikidata"
ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "portalfinder/0.1 (https://github.com/Dhanush-Reddy/Carrier_Portal_finder)"
BUSINESS = "wd:Q4830453"

LIST_QUERY = """
SELECT ?item (MAX(?emp) AS ?employees) WHERE {{
  ?item wdt:P1128 ?emp .
  FILTER(?emp > {low}{high_filter})
  FILTER EXISTS {{ ?item wdt:P31/wdt:P279* {business} . }}
}}
GROUP BY ?item
"""

# Upper bounds of the employee-count bands queried separately; the last band
# is open-ended.
BAND_EDGES = (2_000, 5_000, 10_000, 50_000)


def employee_bands(min_employees: int) -> list[tuple[int, int | None]]:
    """``(low, high]`` bands covering every count above ``min_employees``."""
    edges = [e for e in BAND_EDGES if e > min_employees]
    lows = [min_employees, *edges]
    highs: list[int | None] = [*edges, None]
    return list(zip(lows, highs))

DETAILS_QUERY = """
SELECT ?item ?itemLabel ?website ?countryLabel ?industryLabel ?linkedin
       ?parent ?parentLabel WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P17 ?country . }}
  OPTIONAL {{ ?item wdt:P452 ?industry . }}
  OPTIONAL {{ ?item wdt:P4264 ?linkedin . }}
  OPTIONAL {{ ?item wdt:P749 ?parent . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}}
"""


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _value(binding: dict, key: str) -> str | None:
    cell = binding.get(key)
    return cell["value"] if cell else None


class WikidataClient:
    def __init__(self, client: httpx.Client | None = None, retries: int = 4):
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
            timeout=120,
        )
        self.retries = retries

    def query(self, sparql: str) -> list[dict]:
        delay = 2.0
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.post(ENDPOINT, data={"query": sparql})
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(float(resp.headers.get("Retry-After", delay)))
                    delay *= 2
                    continue
                resp.raise_for_status()
                return resp.json()["results"]["bindings"]
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")


def list_qualifying_ids(
    client: WikidataClient, min_employees: int = MIN_EMPLOYEES, on_band=None
) -> dict[str, int]:
    out: dict[str, int] = {}
    for low, high in employee_bands(min_employees):
        high_filter = f" && ?emp <= {high}" if high is not None else ""
        rows = client.query(LIST_QUERY.format(low=low, high_filter=high_filter, business=BUSINESS))
        found = parse_list(rows, out)
        if on_band:
            on_band(low, high, found)
    return out


def parse_list(rows: list[dict], out: dict[str, int] | None = None) -> int:
    """Add rows to ``out`` (QID -> max employees). Returns rows parsed.

    A company with several reported counts can appear in more than one band;
    the largest count wins.
    """
    out = {} if out is None else out
    for row in rows:
        qid = _qid(row["item"]["value"])
        count = int(float(row["employees"]["value"]))
        out[qid] = max(count, out.get(qid, 0))
    return len(rows)


def parse_details(rows: list[dict], employees: dict[str, int]) -> dict[str, CompanyRecord]:
    """Group SPARQL rows (one per combination of optional values) by item."""
    records: dict[str, CompanyRecord] = {}
    for row in rows:
        qid = _qid(row["item"]["value"])
        rec = records.get(qid)
        if rec is None:
            label = _value(row, "itemLabel")
            # The label service falls back to the bare QID when no label exists.
            name = None if not label or label == qid else label
            rec = records[qid] = CompanyRecord(
                source=SOURCE,
                source_id=qid,
                name=name,
                employee_count=employees.get(qid),
                raw={"websites": [], "countries": [], "industries": [], "linkedin_ids": []},
            )
        raw = rec.raw
        for key, field in (
            ("website", "websites"),
            ("countryLabel", "countries"),
            ("industryLabel", "industries"),
            ("linkedin", "linkedin_ids"),
        ):
            v = _value(row, key)
            if v and v not in raw[field]:
                raw[field].append(v)
        parent_uri = _value(row, "parent")
        if parent_uri:
            pid = _qid(parent_uri)
            if all(p.source_id != pid for p in rec.parents):
                plabel = _value(row, "parentLabel")
                rec.parents.append(Parent(pid, None if plabel == pid else plabel))

    for rec in records.values():
        raw = rec.raw
        # Multiple values are kept in ``raw``; the first is used as the primary.
        rec.website = raw["websites"][0] if raw["websites"] else None
        rec.country = raw["countries"][0] if raw["countries"] else None
        rec.industry = "; ".join(raw["industries"]) or None
        rec.linkedin_url = linkedin_company_url(raw["linkedin_ids"][0]) if raw["linkedin_ids"] else None
        raw["parents"] = [{"id": p.source_id, "name": p.name} for p in rec.parents]
    return records


def fetch_details(
    client: WikidataClient, employees: dict[str, int], batch_size: int = 200
) -> Iterator[CompanyRecord]:
    """Yield exactly one record per ID in ``employees``."""
    ids = sorted(employees)
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        values = " ".join(f"wd:{qid}" for qid in batch)
        rows = client.query(DETAILS_QUERY.format(values=values))
        yield from complete_batch(batch, parse_details(rows, employees), employees)


def complete_batch(
    batch: Iterable[str], found: dict[str, CompanyRecord], employees: dict[str, int]
) -> Iterator[CompanyRecord]:
    for qid in batch:
        yield found.get(qid) or CompanyRecord(
            source=SOURCE,
            source_id=qid,
            name=None,
            employee_count=employees.get(qid),
            raw={"error": "no details returned"},
        )
