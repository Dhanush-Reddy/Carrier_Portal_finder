"""The common shape every source is converted into before ingest."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Parent:
    source_id: str
    name: str | None = None


@dataclass
class CompanyRecord:
    source: str
    source_id: str
    name: str | None
    employee_count: int | None
    linkedin_url: str | None = None
    website: str | None = None
    country: str | None = None
    industry: str | None = None
    parents: list[Parent] = field(default_factory=list)
    dissolved: str | None = None  # date the company ceased to exist, if it has
    raw: dict | None = None
