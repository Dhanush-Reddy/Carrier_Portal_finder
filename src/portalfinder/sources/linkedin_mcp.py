"""Fetch company details through a LinkedIn MCP server you run yourself.

The server is started the same way Claude Desktop or Claude Code starts it,
from its entry in their MCP config (or from a command line). For each input
company the server's company-profile tool is called once, the answer is
cached on disk (so an interrupted run resumes where it stopped), and the
details are parsed into one CSV row per company.

Most LinkedIn MCP servers act as your logged-in LinkedIn account, and
LinkedIn restricts accounts that read many pages quickly. So calls are
spaced out (``delay``), a run stops after ``limit`` calls, and it stops at
once when the server reports a block, a challenge or a sign-in problem.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import shlex
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from portalfinder.normalize import linkedin_company_url
from portalfinder.records import CompanyRecord

SOURCE = "linkedin"

OUT_COLUMNS = [
    "input_name", "status", "error", "name", "linkedin_url", "website", "industry",
    "employee_band", "employee_count", "headquarters", "country", "founded",
]

# Words in an error that mean LinkedIn (or the server) is pushing back; the
# run stops instead of trying the next company.
STOP_RE = re.compile(
    r"rate.?limit|too many requests|\b429\b|captcha|challenge|checkpoint|"
    r"log ?in|sign ?in|session|cookie|auth|unauthori[sz]ed|forbidden|blocked|restricted",
    re.IGNORECASE)
MAX_CONSECUTIVE_ERRORS = 3


class McpSetupError(RuntimeError):
    """The server or tool could not be found or started."""


# --- Server config ---------------------------------------------------------

def _find_servers(node) -> dict[str, dict]:
    """Every ``mcpServers`` entry anywhere in a config file (Claude Code keeps
    them per project as well as at the top level)."""
    found: dict[str, dict] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "mcpServers" and isinstance(value, dict):
                found.update({k: v for k, v in value.items() if isinstance(v, dict)})
            else:
                found.update(_find_servers(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_find_servers(item))
    return found


def load_server(config_path: str | Path, name: str | None = None) -> dict:
    """The server entry from a Claude Desktop / Claude Code MCP config.

    Without ``name``, the one entry with "linkedin" in its name is used.
    """
    servers = _find_servers(json.loads(Path(config_path).read_text(encoding="utf-8")))
    if not servers:
        raise McpSetupError(f"no mcpServers in {config_path}")
    if name:
        if name not in servers:
            raise McpSetupError(f"no server {name!r} in {config_path}; found: {', '.join(servers)}")
        return servers[name]
    linkedin = [k for k in servers if "linkedin" in k.lower()]
    if len(linkedin) != 1:
        raise McpSetupError(
            f"pick a server with --server; found: {', '.join(servers)}")
    return servers[linkedin[0]]


def server_from_command(command: str) -> dict:
    parts = shlex.split(command, posix=os.name != "nt")
    if not parts:
        raise McpSetupError("empty --mcp-command")
    return {"command": parts[0], "args": parts[1:]}


@asynccontextmanager
async def open_session(server: dict):
    """An initialised MCP ``ClientSession`` for a config entry."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise McpSetupError(
            'the MCP client is not installed; run: python -m pip install -e ".[linkedin]"') from exc

    url = server.get("url")
    if url:
        headers = server.get("headers") or None
        if server.get("type") == "sse" or url.rstrip("/").endswith("/sse"):
            from mcp.client.sse import sse_client
            transport = sse_client(url, headers=headers)
        else:
            from mcp.client.streamable_http import streamablehttp_client
            transport = streamablehttp_client(url, headers=headers)
        async with transport as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session
        return

    if not server.get("command"):
        raise McpSetupError("the server entry has neither a command nor a url")
    params = StdioServerParameters(
        command=server["command"], args=list(server.get("args") or []),
        env={**os.environ, **(server.get("env") or {})}, cwd=server.get("cwd"))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# --- Tool and argument choice ---------------------------------------------

@dataclass
class ToolInfo:
    name: str
    description: str
    properties: dict
    required: list[str]


async def list_tools(server: dict) -> list[ToolInfo]:
    async with open_session(server) as session:
        result = await session.list_tools()
    return [
        ToolInfo(t.name, t.description or "", (t.inputSchema or {}).get("properties") or {},
                 list((t.inputSchema or {}).get("required") or []))
        for t in result.tools
    ]


def pick_tool(tools: list[ToolInfo], name: str | None = None) -> ToolInfo:
    """The named tool, or the one tool that looks up a single company."""
    if name:
        for tool in tools:
            if tool.name == name:
                return tool
        raise McpSetupError(f"no tool {name!r}; the server has: {', '.join(t.name for t in tools)}")
    candidates = [
        t for t in tools
        if "company" in t.name.lower()
        and not re.search(r"search|job|employee|people|post|update|follow", t.name, re.IGNORECASE)
    ]
    if len(candidates) != 1:
        raise McpSetupError(
            "pick the company-profile tool with --tool; the server has: "
            + ", ".join(t.name for t in tools))
    return candidates[0]


URL_ARG_RE = re.compile(r"url|link", re.IGNORECASE)
NAME_ARG_RE = re.compile(r"company|name|slug|id|identifier|username|query", re.IGNORECASE)


def pick_argument(tool: ToolInfo, name: str | None = None) -> str:
    """Which input of the tool takes the company (its URL, or its name/slug)."""
    if name:
        if tool.properties and name not in tool.properties:
            raise McpSetupError(
                f"tool {tool.name} has no input {name!r}; it takes: {', '.join(tool.properties)}")
        return name
    props = [p for p in tool.properties if p in tool.required] or list(tool.properties)
    for pattern in (URL_ARG_RE, NAME_ARG_RE):
        for prop in props:
            if pattern.search(prop):
                return prop
    if len(props) == 1:
        return props[0]
    raise McpSetupError(
        f"pick the input that takes the company with --arg; {tool.name} takes: "
        + ", ".join(tool.properties))


def linkedin_slug(url: str | None) -> str | None:
    canonical = linkedin_company_url(url)
    return canonical.rsplit("/", 1)[-1] if canonical else None


def argument_value(arg: str, company: dict) -> str | None:
    """A URL for a URL input, else the LinkedIn slug (falling back to the name)."""
    url = linkedin_company_url(company.get("linkedin_url"))
    if URL_ARG_RE.search(arg):
        return url
    return linkedin_slug(url) or company.get("name")


# --- Parsing the answer ----------------------------------------------------

def result_text(result) -> str:
    """All text in a tool result, plus its structured content as JSON."""
    parts = []
    structured = getattr(result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured))
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


BAND_RE = re.compile(
    r"([\d,.]+)\s*(?:-|–|to)\s*([\d,.]+)\s*(?:employees)?|([\d,.]+)\s*\+\s*(?:employees)?",
    re.IGNORECASE)

# "Label\nvalue" pairs on LinkedIn's About page.
TEXT_FIELDS = {
    "website": r"website",
    "industry": r"industry|industries",
    "company_size": r"company size|size",
    "headquarters": r"headquarters",
    "founded": r"founded",
}

JSON_KEYS = {
    "name": ("name", "company_name", "companyname", "title"),
    "website": ("website", "website_url", "websiteurl", "company_website"),
    "industry": ("industry", "industries", "industry_name"),
    "company_size": ("company_size", "companysize", "size", "employee_count_range",
                     "staff_count_range", "employees_range"),
    "employee_count": ("staff_count", "staffcount", "employee_count", "employeecount",
                       "employees", "headcount"),
    "headquarters": ("headquarters", "hq", "headquarter", "location"),
    "founded": ("founded", "founded_year", "foundedon"),
    "linkedin_url": ("linkedin_url", "url", "company_url", "linkedinurl"),
}


def _walk(node, out: dict[str, object]) -> None:
    """Collect the first value of each known key anywhere in a JSON answer."""
    if isinstance(node, dict):
        # This level's keys first, so a nested {"name": ...} doesn't win.
        for key, value in node.items():
            for field, names in JSON_KEYS.items():
                if key.lower() in names and field not in out and value not in (None, "", [], {}):
                    out[field] = value
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def _as_text(value) -> str:
    if isinstance(value, list):
        return "; ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        parts = [str(value[k]) for k in ("city", "geographicArea", "region", "country", "name")
                 if value.get(k)]
        return ", ".join(parts) or json.dumps(value)
    return str(value).strip()


def _number(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def employee_band(text: str | None) -> tuple[str | None, int | None]:
    """("1,001-5,000", 1001) from "1,001-5,000 employees"; the band's lower bound."""
    if not text:
        return None, None
    m = BAND_RE.search(text)
    if not m:
        n = _number(text)
        return (text.strip(), n) if n else (None, None)
    if m.group(3):
        return f"{m.group(3)}+", _number(m.group(3))
    return f"{m.group(1)}-{m.group(2)}", _number(m.group(1))


def parse_company(text: str) -> dict:
    """Company details from a tool's answer, JSON or LinkedIn page text."""
    found: dict[str, object] = {}
    for chunk in _json_chunks(text):
        _walk(chunk, found)
    details = {k: _as_text(v) for k, v in found.items()}
    lines = [ln.strip() for ln in text.replace("\\n", "\n").splitlines()]
    for field, label in TEXT_FIELDS.items():
        if details.get(field):
            continue
        label_re = re.compile(rf"^(?:{label})\s*:?\s*(.*)$", re.IGNORECASE)
        for i, line in enumerate(lines):
            m = label_re.match(line)
            if m:
                value = m.group(1) or next((ln for ln in lines[i + 1:] if ln), "")
                if value:
                    details[field] = value
                    break
    band, count = employee_band(details.get("company_size"))
    if details.get("employee_count"):
        count = _number(details["employee_count"]) or count
    details["employee_band"] = band
    details["employee_count"] = count
    details.pop("company_size", None)
    return details


def _json_chunks(text: str) -> Iterable:
    for line in [text, *text.splitlines()]:
        line = line.strip()
        if line[:1] in "{[":
            try:
                yield json.loads(line)
            except ValueError:
                continue


# --- Country from the headquarters ----------------------------------------

INDIAN_PLACES = {
    "andhra pradesh", "assam", "bihar", "chhattisgarh", "delhi", "new delhi", "goa", "gujarat",
    "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh",
    "maharashtra", "odisha", "punjab", "rajasthan", "tamil nadu", "telangana",
    "uttar pradesh", "uttarakhand", "west bengal", "chandigarh", "bengaluru", "bangalore",
    "mumbai", "pune", "hyderabad", "chennai", "noida", "gurugram", "gurgaon", "kolkata",
}
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
    "va", "wa", "wv", "wi", "wy", "dc", "california", "new york", "texas", "washington",
    "massachusetts", "illinois", "new jersey", "georgia", "virginia", "north carolina",
}


def country_from(headquarters: str | None, known: dict[str, str]) -> str | None:
    """The country in "Bengaluru, Karnataka" or "Austin, Texas, US", if clear.

    ``known`` maps lower-cased country names to how the database spells them.
    """
    if not headquarters:
        return None
    parts = [p.strip().lower() for p in headquarters.split(",") if p.strip()]
    for part in reversed(parts):
        if part in known:
            return known[part]
        if part in INDIAN_PLACES or part == "india":
            return "India"
        if part in US_STATES or part in {"us", "usa", "united states"}:
            return "United States"
    return None


# --- Fetching --------------------------------------------------------------

def read_companies(path: str | Path) -> list[dict]:
    """Rows with name / linkedin_url / website / country (headers matched loosely)."""
    aliases = {
        "name": ("name", "company", "company_name", "input_name"),
        "linkedin_url": ("linkedin_url", "linkedin", "linkedin_company_url", "url"),
        "website": ("website", "domain", "website_url"),
        "country": ("country",),
    }
    rows = []
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            row = {field: next((lower[a] for a in names if lower.get(a)), "")
                   for field, names in aliases.items()}
            if row["name"] or row["linkedin_url"]:
                rows.append(row)
    return rows


def cache_key(company: dict) -> str:
    key = linkedin_slug(company.get("linkedin_url")) or company.get("name") or ""
    return re.sub(r"[^a-z0-9._-]+", "_", key.lower()).strip("_") or "unnamed"


@dataclass
class FetchSummary:
    ok: int = 0
    not_found: int = 0
    errors: int = 0
    not_fetched: int = 0
    calls: int = 0
    stopped: str | None = None


async def fetch_companies(
    server: dict,
    companies: list[dict],
    out_csv: str | Path,
    cache_dir: str | Path,
    tool_name: str | None = None,
    arg_name: str | None = None,
    delay: float = 20.0,
    limit: int | None = 150,
    countries: Iterable[str] = (),
    on_progress: Callable[[str], None] = lambda _msg: None,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> tuple[FetchSummary, list[dict]]:
    """Call the server once per company not cached yet; write every row."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    known = {c.lower(): c for c in countries if c}
    summary = FetchSummary()
    rows: list[dict] = []
    consecutive_errors = 0

    async with open_session(server) as session:
        tools = await session.list_tools()
        tool = pick_tool([ToolInfo(t.name, t.description or "",
                                   (t.inputSchema or {}).get("properties") or {},
                                   list((t.inputSchema or {}).get("required") or []))
                          for t in tools.tools], tool_name)
        arg = pick_argument(tool, arg_name)
        on_progress(f"using tool {tool.name}({arg}=...)")

        for i, company in enumerate(companies, 1):
            cached = cache / f"{cache_key(company)}.json"
            text = error = None
            if cached.exists():
                text = json.loads(cached.read_text(encoding="utf-8"))["text"]
            elif summary.stopped or (limit is not None and summary.calls >= limit):
                rows.append(_row(company, "not_fetched", summary.stopped or "run limit reached"))
                summary.not_fetched += 1
                continue
            else:
                value = argument_value(arg, company)
                if not value:
                    rows.append(_row(company, "error", f"no value for {arg}"))
                    summary.errors += 1
                    continue
                if summary.calls:
                    await sleep(delay * random.uniform(0.7, 1.3))
                summary.calls += 1
                try:
                    result = await session.call_tool(tool.name, {arg: value})
                    text = result_text(result)
                    if getattr(result, "isError", False):
                        error, text = text or "tool error", None
                except Exception as exc:  # the server's failure, reported per company
                    error = f"{type(exc).__name__}: {exc}"
                if text is not None:
                    cached.write_text(json.dumps({"input": company, "text": text}), encoding="utf-8")
            if error:
                rows.append(_row(company, "error", error[:300]))
                summary.errors += 1
                consecutive_errors += 1
                if STOP_RE.search(error):
                    summary.stopped = f"stopped: server said {error[:120]!r}"
                elif consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    summary.stopped = f"stopped after {consecutive_errors} errors in a row"
                if summary.stopped:
                    on_progress(summary.stopped)
                continue
            consecutive_errors = 0
            details = parse_company(text or "")
            if not any(details.get(k) for k in ("industry", "employee_band", "employee_count", "website")):
                rows.append(_row(company, "not_found", (text or "empty answer")[:300]))
                summary.not_found += 1
            else:
                rows.append(_row(company, "ok", "", details, known))
                summary.ok += 1
            if i % 10 == 0:
                on_progress(f"  {i}/{len(companies)} done ({summary.calls} calls this run)")

    write_rows(out_csv, rows)
    return summary, rows


def _row(company: dict, status: str, error: str, details: dict | None = None,
         known: dict[str, str] | None = None) -> dict:
    details = details or {}
    hq = details.get("headquarters")
    return {
        "input_name": company.get("name") or "",
        "status": status,
        "error": error,
        "name": details.get("name") or company.get("name") or "",
        "linkedin_url": linkedin_company_url(details.get("linkedin_url"))
        or linkedin_company_url(company.get("linkedin_url")) or "",
        "website": details.get("website") or company.get("website") or "",
        "industry": details.get("industry") or "",
        "employee_band": details.get("employee_band") or "",
        "employee_count": details.get("employee_count") or "",
        "headquarters": hq or "",
        "country": company.get("country") or country_from(hq, known or {}) or "",
        "founded": details.get("founded") or "",
    }


def write_rows(path: str | Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def to_records(rows: Iterable[dict]) -> Iterable[CompanyRecord]:
    """Fetched companies as ingest records (the band's lower bound as the count)."""
    for row in rows:
        if row["status"] != "ok":
            continue
        count = row["employee_count"]
        yield CompanyRecord(
            source=SOURCE,
            source_id=linkedin_slug(row["linkedin_url"]) or row["name"].lower(),
            name=row["name"],
            employee_count=int(count) if str(count).isdigit() else None,
            linkedin_url=row["linkedin_url"] or None,
            website=row["website"] or None,
            country=row["country"] or None,
            industry=row["industry"] or None,
            raw=row,
        )
