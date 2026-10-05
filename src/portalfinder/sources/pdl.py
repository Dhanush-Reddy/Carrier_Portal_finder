"""People Data Labs free company dataset.

The dataset (https://docs.peopledatalabs.com/docs/free-company-dataset) is
one large file of about 22 million companies, in CSV, pipe-delimited or JSON
lines, usually zipped. Each record has ``name``, ``website``, ``industry``,
``size`` (a band such as "1001-5000" or "10001+"), ``locality``, ``region``,
``country``, ``founded``, ``linkedin_url`` and ``id``, all lower case.

The file is streamed, so it is never loaded into memory; only records in the
wanted countries and size bands become ingest records. The band's lower
bound is used as the employee count ("1001-5000" becomes 1001).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from portalfinder.records import CompanyRecord

SOURCE = "pdl"

# The dataset's size bands, smallest first.
SIZE_BANDS = ("1-10", "11-50", "51-200", "201-500", "501-1000",
              "1001-5000", "5001-10000", "10001+")

# Words kept in capitals when a lower-case name is title-cased.
_UPPER = {"it", "ai", "hr", "llp", "llc", "plc", "ag", "se", "sa", "nv", "bv",
          "usa", "uk", "ibm", "hcl", "tcs", "sap", "bpo", "kpo"}
_KEEP_LOWER = {"and", "of", "the", "for", "in", "de", "la", "du", "von", "&"}


def band_lower_bound(size: str | None) -> int | None:
    """1001 for "1001-5000", 10001 for "10001+"."""
    m = re.match(r"\s*([\d,]+)", size or "")
    return int(m.group(1).replace(",", "")) if m else None


def nice_name(name: str) -> str:
    """'tata consultancy services pvt ltd' -> 'Tata Consultancy Services Pvt Ltd'."""
    if not name or name != name.lower():
        return name
    words = []
    for i, word in enumerate(name.split()):
        if word in _UPPER:
            words.append(word.upper())
        elif i and word in _KEEP_LOWER:
            words.append(word)
        else:
            # Capitals after - & / . too: "l&t" -> "L&T", "coca-cola" -> "Coca-Cola".
            words.append(re.sub(r"(^|[-&/.])([a-z])", lambda m: m.group(1) + m.group(2).upper(), word))
    return " ".join(words)


def nice_place(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(w if w in _KEEP_LOWER else w[:1].upper() + w[1:] for w in value.split())


def _open_text(path: Path) -> io.TextIOBase:
    """The dataset as text, from a plain, .gz or .zip file (first data member)."""
    if path.suffix.lower() == ".zip":
        archive = zipfile.ZipFile(path)
        members = [m for m in archive.infolist()
                   if not m.is_dir() and not m.filename.startswith("__MACOSX")]
        if not members:
            raise ValueError(f"{path} is an empty zip file")
        member = max(members, key=lambda m: m.file_size)
        return io.TextIOWrapper(archive.open(member), encoding="utf-8", errors="replace", newline="")
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open(encoding="utf-8", errors="replace", newline="")


def iter_rows(path: str | Path) -> Iterator[dict]:
    """Every record in the file as a dict, whatever its format."""
    fh = _open_text(Path(path))
    with fh:
        first = fh.readline()
        if not first:
            return
        if first.lstrip().startswith(("{", "[")):
            yield from _json_rows(first, fh)
            return
        delimiter = "|" if first.count("|") > first.count(",") else ","
        csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
        reader = csv.reader(fh, delimiter=delimiter)
        header = [h.strip().lower().lstrip("﻿") for h in next(csv.reader([first], delimiter=delimiter))]
        for values in reader:
            if values:
                yield dict(zip(header, values))


def _json_rows(first: str, rest: Iterable[str]) -> Iterator[dict]:
    if first.lstrip().startswith("["):
        # One JSON array: small exports only, since it has to be read whole.
        data = json.loads(first + "".join(rest))
        yield from (row for row in data if isinstance(row, dict))
        return
    for line in [first, *rest]:
        line = line.strip().rstrip(",")
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


@dataclass
class ScanSummary:
    read: int = 0
    kept: int = 0
    by_country: dict[str, int] = field(default_factory=dict)


def records(
    path: str | Path,
    countries: Iterable[str] = (),
    min_employees: int = 1000,
    summary: ScanSummary | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Iterator[CompanyRecord]:
    """Ingest records for companies in ``countries`` (any case; all when empty)
    whose size band starts above ``min_employees``."""
    wanted = {c.strip().lower() for c in countries if c.strip()}
    bands = {b for b in SIZE_BANDS if (band_lower_bound(b) or 0) > min_employees}
    summary = summary if summary is not None else ScanSummary()
    for row in iter_rows(path):
        summary.read += 1
        if on_progress and summary.read % 1_000_000 == 0:
            on_progress(summary.read, summary.kept)
        country = (row.get("country") or "").strip().lower()
        if wanted and country not in wanted:
            continue
        size = (row.get("size") or "").strip().lower()
        if size not in bands:
            continue
        summary.kept += 1
        summary.by_country[country] = summary.by_country.get(country, 0) + 1
        yield to_record(row)


def to_record(row: dict) -> CompanyRecord:
    website = (row.get("website") or "").strip()
    if website and "://" not in website:
        website = "https://" + website
    linkedin = (row.get("linkedin_url") or "").strip()
    if linkedin and "://" not in linkedin:
        linkedin = "https://www." + linkedin.removeprefix("www.")
    name = (row.get("name") or "").strip()
    return CompanyRecord(
        source=SOURCE,
        source_id=(row.get("id") or linkedin or website or name).strip(),
        name=nice_name(name) or None,
        employee_count=band_lower_bound(row.get("size")),
        linkedin_url=linkedin or None,
        website=website or None,
        country=nice_place(row.get("country")),
        industry=(row.get("industry") or "").strip() or None,
        raw={k: row.get(k) for k in ("id", "name", "size", "industry", "locality", "region",
                                     "country", "founded", "website", "linkedin_url")},
    )
