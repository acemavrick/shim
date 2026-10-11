import asyncio
import json
from importlib import import_module
from urllib.parse import parse_qs

import httpx

from shim.config import Config
from shim.manifest import Action, Extract, FromInput, FromPage, HttpBackend, InputSchema
from shim.runtime import http
from shim.runtime.extract import run_extract


def test_compiled_selectors_reuse_nodes_and_preserve_field_order(monkeypatch):
    extract = import_module("shim.runtime.extract")
    original = extract._css_selector
    selections = []

    class Selector:
        def __init__(self, raw):
            self.compiled = original(raw)
            self.raw = raw

        def select_one(self, scope):
            selections.append(self.raw)
            return self.compiled.select_one(scope)

    monkeypatch.setattr(extract, "_css_selector", lambda raw: Selector(raw))
    spec = Extract(items="li", fields={"text": "a", "root": "@data-id", "url": "a@href", "class": "a@class"})
    result = run_extract('<li data-id="1"><a href="../next" class="one two"> A\nB </a></li>',
                         spec, "https://site.test/catalog/page")
    assert result.records == [{"text": "A B", "root": "1", "url": "https://site.test/next", "class": "one two"}]
    assert list(result.records[0]) == ["text", "root", "url", "class"]
    assert selections == ["a"]


def test_selector_cache_does_not_cache_response_data_or_mutated_spec():
    spec = Extract(items="li", fields={"name": "a", "bad": "[invalid"})
    first = run_extract("<li><a>First</a></li>", spec)
    second = run_extract("<li><a>Second</a></li>", spec)
    assert first.records == [{"name": "First", "bad": None}]
    assert second.records == [{"name": "Second", "bad": None}]
    spec.fields["name"] = "b"
    assert run_extract("<li><b>Updated</b></li>", spec).records[0]["name"] == "Updated"


def test_cached_json_paths_preserve_native_types_and_path_edge_cases():
    item = {"nested": {"amount": "$1,249.50"}, "rows": [False, {"value": 3}], "": "empty key"}
    spec = Extract(kind="json", fields={"root": "", "amount": "nested.amount|number", "last": "rows.-1.value",
                                       "bool": "rows.0", "invalid": "rows.bad", "absent": "nested.absent",
                                       "too_far": "rows.2", "null": "null.more", "empty_segment": "."})
    result = run_extract(json.dumps(item), spec)
    assert result.records == [{"root": item, "amount": 1249.5, "last": 3, "bool": False,
                               "invalid": None, "absent": None, "too_far": None, "null": None,
                               "empty_segment": None}]
    assert run_extract('[true,7,null]', Extract(kind="json", fields={"value": ""})).records == [
        {"value": True}, {"value": 7}, {"value": None}]


def test_independent_calls_overlap_and_return_fresh_responses(monkeypatch):
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: Config(min_request_gap=0))
    generations = 0
    active = peak = 0

    async def run():
        both_started = asyncio.Event()

        async def handle(request):
            nonlocal generations, active, peak
            generations += 1
            generation = generations
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            try:
                await asyncio.wait_for(both_started.wait(), timeout=1)
                return httpx.Response(200, json={"generation": generation})
            finally:
                active -= 1

        action = Action(name="read", description="Read", tier="read", input=InputSchema(),
                        backend=HttpBackend(method="GET", url="https://site.test/value"),
                        extract=Extract(kind="json", fields={"generation": "generation"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(http, "_get_client", lambda: client)
            return await asyncio.gather(http.invoke_action(action, {}), http.invoke_action(action, {}))

    results = asyncio.run(run())
    assert peak == 2 and generations == 2
    assert {r.records[0]["generation"] for r in results} == {1, 2}


def test_post_refetches_rotating_hidden_fields_before_each_submission(monkeypatch):
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: Config(min_request_gap=0))
    generation = 0
    submissions = []

    def handle(request):
        nonlocal generation
        if request.method == "GET":
            generation += 1
            body = ('<html><head><title>Form</title></head><body>' + '<p>Unrelated content</p>' * 1000
                    + f'<form><input type="hidden" name="csrf" value="token-{generation}">'
                    '<input type="hidden" name="empty"><input name="q" value="ignored">'
                    '<input type="hidden" value="nameless"></form></body></html>')
            return httpx.Response(200, text=body)
        params = parse_qs(request.content.decode(), keep_blank_values=True)
        submissions.append(params)
        assert request.headers["referer"] == "https://site.test/form"
        assert request.headers["origin"] == "https://site.test"
        return httpx.Response(200, json={"received": params["csrf"][0]})

    backend = HttpBackend(method="POST", url="https://site.test/submit", encoding="form",
                          refetch_from="https://site.test/form",
                          fields={"csrf": FromPage(field="csrf"), "empty": FromPage(field="empty"),
                                  "q": FromInput(name="q")})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(http, "_get_client", lambda: client)
            for q in ["First", "Second"]:
                await http.send_http(backend, {"q": q})

    asyncio.run(run())
    assert generation == 2
    assert submissions == [{"csrf": ["token-1"], "empty": [""], "q": ["First"]},
                           {"csrf": ["token-2"], "empty": [""], "q": ["Second"]}]
