import json

import httpx

from conftest import FIXTURES, load_fixture
from portalfinder.sources import wikidata


def test_parse_list():
    ids = wikidata.parse_list(load_fixture("wikidata_list.json"))
    assert ids["Q312"] == 161000
    assert len(ids) == 4


def test_parse_details_groups_rows_and_keeps_all_values():
    ids = wikidata.parse_list(load_fixture("wikidata_list.json"))
    recs = wikidata.parse_details(load_fixture("wikidata_details.json"), ids)
    apple = recs["Q312"]
    assert apple.name == "Apple Inc."
    assert apple.employee_count == 161000
    assert apple.linkedin_url == "https://www.linkedin.com/company/apple"
    assert apple.industry == "consumer electronics; software industry"
    google = recs["Q95"]
    assert [(p.source_id, p.name) for p in google.parents] == [("Q20800404", "Alphabet Inc.")]


def test_fetch_details_returns_one_record_per_id_even_when_missing():
    list_body = (FIXTURES / "wikidata_list.json").read_text()
    details_body = (FIXTURES / "wikidata_details.json").read_text()
    calls = []

    def handler(request):
        query = request.content.decode()
        calls.append(query)
        return httpx.Response(200, text=details_body if "VALUES" in query else list_body)

    client = wikidata.WikidataClient(httpx.Client(transport=httpx.MockTransport(handler)))
    ids = wikidata.list_qualifying_ids(client)
    recs = list(wikidata.fetch_details(client, ids, batch_size=2))
    assert sorted(r.source_id for r in recs) == sorted(ids)
    missing = [r for r in recs if r.source_id == "Q999999999"]
    assert missing[0].name is None
    assert len(calls) == 3  # one list query, two detail batches


def test_query_retries_on_429(monkeypatch):
    monkeypatch.setattr(wikidata.time, "sleep", lambda s: None)
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, text=json.dumps({"results": {"bindings": []}})),
    ])
    client = wikidata.WikidataClient(
        httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    )
    assert client.query("ASK {}") == []
