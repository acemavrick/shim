import asyncio
import json

from shim.explore.agent import AgentExplorer, AgentResult, Observed
from shim.manifest import Action, Extract, FromInput, HttpBackend, InputSchema, PropertySchema


def test_bad_candidate_does_not_erase_briefing_or_good_sibling():
    async def complete(system, prompt):
        return json.dumps({"site_description": "A public library", "notes": ["Browse books"],
                           "tools": [{"name": "get_books", "description": "List books", "tier": "read",
                                      "method": "GET", "url": "https://x.test/books"},
                                     {"name": "bad", "tier": "nonsense"}]})
    result = AgentResult(observed=[Observed(step=1, method="GET", url="https://x.test/books",
                                           resource_type="document", status=200, content_type="text/html")])
    asyncio.run(AgentExplorer(complete, "Learn site")._write_down(result))
    assert result.site_description == "A public library"
    assert [a.name for a in result.tools] == ["get_books"]
    assert result.errors
    assert result.model_trace


def test_typed_sample_variants():
    from shim.check import sample_args
    a = Action(name="list_books", description="List", tier="read", input=InputSchema(properties={
        "page": PropertySchema(type="integer", examples=["1", "2"]),
        "available": PropertySchema(type="boolean", examples=["false", "true"]),
        "category": PropertySchema(type="string", enum=["fiction", "history"]),
    }), backend=HttpBackend(method="GET", url="https://x.test/books"), extract=Extract())
    assert sample_args(a, "x") == {"page": 1, "available": False, "category": "fiction"}
    assert sample_args(a, "x", 1) == {"page": 2, "available": True, "category": "history"}


def test_read_post_policy_is_exact_and_operator_owned():
    from shim.safety import post_is_approved, ReadPostRule
    rules = [ReadPostRule(url="https://x.test/api", body={"operation": "search"})]
    assert post_is_approved("https://x.test/api", {"operation": "search"}, rules)
    assert not post_is_approved("https://x.test/api", {"operation": "delete"}, rules)
    assert not post_is_approved("https://evil.test/api", {"operation": "search"}, rules)
    assert not post_is_approved("https://x.test/api", {"operation": "search", "delete": True}, rules)


def test_history_preserves_versions(tmp_path, monkeypatch):
    import shim.compile as compiler
    from shim.history import new_run, publish_run, load_version, list_versions
    from shim.manifest import Manifest, Coverage
    monkeypatch.setattr(compiler, "SITES_DIR", tmp_path)
    async def run():
        first, directory = new_run("x.test")
        m = Manifest(site="https://x.test", host="x.test", compiled_at="first", run_id=first, coverage=Coverage())
        await publish_run(m, directory)
        second, directory = new_run("x.test")
        m2 = m.model_copy(update={"run_id": second, "compiled_at": "second"})
        await publish_run(m2, directory)
        assert load_version("x.test", first).compiled_at == "first"
        assert compiler.load_manifest("x.test").run_id == second
        assert len(list_versions("x.test")) == 2
        assert (tmp_path / "x.test" / "history" / first / "agent" / "llms.txt").exists()
    asyncio.run(run())


def test_browser_recipe_schema_rejects_code_and_excess_steps():
    import pytest
    from pydantic import ValidationError
    from shim.manifest import BrowserBackend
    with pytest.raises(ValidationError):
        BrowserBackend(start_url="https://x.test", steps=[{"action": "evaluate", "text": "alert(1)"}])
    with pytest.raises(ValidationError):
        BrowserBackend(start_url="https://x.test", steps=[{"action": "back"}] * 13)


def test_browser_replays_with_two_inputs_in_fresh_sessions(monkeypatch):
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from shim.manifest import BrowserBackend
    from shim.runtime.browser import replay_browser
    from shim.runtime.http import invoke_action
    html = b'''<main><label>Search<input id="query"></label><button onclick="document.querySelector('article').textContent=document.querySelector('input').value">Find</button><article></article></main>'''
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    backend = BrowserBackend(start_url=url, steps=[
        {"action": "fill", "role": "textbox", "name": "Search", "text": "{query}"},
        {"action": "click", "role": "button", "name": "Find"}])
    action = Action(name="search", description="Search", tier="read", backend=backend,
        input=InputSchema(properties={"query": PropertySchema(type="string", examples=["river", "forest"])}, required=["query"]),
        extract=Extract(fields={"content": "article"}))
    async def run():
        from shim.check import check_action
        from shim.translate.describe import Proposal
        async def describe(*args):
            return Proposal(extract=action.extract)
        checked, skip = await check_action(action, ["river", "forest"], describer=describe)
        assert skip is None and checked.check.passed
        assert checked.check.control_input == {"query": "forest"}
        assert (await invoke_action(checked, {"query": "ocean"})).records == [{"content": "ocean"}]
        import pytest
        with pytest.raises(ValueError, match="Missing browser input"):
            await replay_browser(backend, {})
        with pytest.raises(ValueError, match="outside its origin"):
            await replay_browser(BrowserBackend(start_url=url, steps=[{"action": "goto", "url": "https://other.test/"}]), {})
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_invalid_examples_and_variable_constants_are_rejected():
    from shim.explore.agent import _ToolSpec
    from shim.explore.writedown import validate_inputs
    observations = [Observed(step=1, method="GET", url=f"https://x.test/search?q={q}", resource_type="fetch",
                             status=200, content_type="application/json") for q in ("river", "forest")]
    spec = _ToolSpec(name="search", description="Search", tier="read", url="https://x.test/search",
                     inputs={"query": {"wire": "q", "examples": ["invented"]}})
    assert "unobserved" in validate_inputs(spec, observations)
    spec.inputs = {}
    spec.constants = {"q": "river"}
    assert "constant" in validate_inputs(spec, observations)


def test_failed_publication_keeps_previous_version(tmp_path, monkeypatch):
    import shim.compile as compiler
    import shim.emit as emitter
    from shim.history import new_run, publish_run, list_versions
    from shim.manifest import Manifest
    monkeypatch.setattr(compiler, "SITES_DIR", tmp_path)
    async def run():
        first, directory = new_run("x.test")
        m = Manifest(site="https://x.test", host="x.test", compiled_at="first", run_id=first)
        await publish_run(m, directory)
        second, directory = new_run("x.test")
        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(emitter, "emit_all", fail)
        import pytest
        with pytest.raises(OSError):
            await publish_run(m.model_copy(update={"run_id": second}), directory)
        assert compiler.load_manifest("x.test").run_id == first
        assert len(list_versions("x.test")) == 1
    asyncio.run(run())


def test_read_post_does_not_follow_redirects(monkeypatch):
    import httpx
    from shim.runtime import http
    from shim.config import Config
    from shim.safety import ReadPostRule
    seen = []
    def transport(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.test/delete"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True)
    monkeypatch.setattr(http, "_get_client", lambda: client)
    import shim.config as cfg
    monkeypatch.setattr(cfg, "config", lambda: Config(read_post_rules=[ReadPostRule(url="https://x.test/approved", body={})]))
    action = Action(name="list_items", description="List", tier="read", input=InputSchema(),
                    backend=HttpBackend(method="POST", url="https://x.test/approved"), extract=Extract())
    async def run():
        try:
            result = await http.invoke_action(action, {})
            assert result.status == 307
        finally:
            await client.aclose()
    asyncio.run(run())
    assert seen == ["https://x.test/approved"]


def test_malformed_placeholder_is_isolated_from_good_candidates():
    async def complete(system, prompt):
        return json.dumps({"site_description": "Library", "tools": [
            {"name": "bad", "description": "Bad", "tier": "read", "url": "https://x.test/books/{book-id}"},
            {"name": "list_books", "description": "List", "tier": "read", "url": "https://x.test/books/one"}]})
    result = AgentResult(observed=[Observed(step=1, method="GET", url="https://x.test/books/one",
                            resource_type="document", status=200, content_type="text/html")])
    asyncio.run(AgentExplorer(complete, "Learn")._write_down(result))
    assert [a.name for a in result.tools] == ["list_books"]
    assert result.site_description == "Library"


def test_streamed_model_tools_round_trip_parameter_sources():
    events = []
    async def complete(system, prompt):
        return json.dumps({"tools": [{"name": "search", "description": "Search", "tier": "read",
            "url": "https://x.test/search", "inputs": {"query": {"wire": "q", "examples": ["river", "forest"]}}}]})
    result = AgentResult(observed=[Observed(step=1, method="GET", url=f"https://x.test/search?q={q}",
                         resource_type="fetch", status=200, content_type="application/json") for q in ("river", "forest")])
    asyncio.run(AgentExplorer(complete, "Read", on_event=events.append)._write_down(result))
    definition = next(e["definition"] for e in events if e["type"] == "agent_tool")
    assert Action.model_validate(definition).backend.fields["q"].name == "query"


def test_provider_failure_does_not_repeat_across_capability_batches():
    calls = []
    async def complete(system, prompt):
        calls.append(prompt)
        raise RuntimeError("provider authentication failed")
    result = AgentResult(observed=[Observed(step=1, method="GET", url="https://x.test/books",
                         resource_type="document", status=200, content_type="text/html")])
    asyncio.run(AgentExplorer(complete, "Read")._write_down(result))
    assert len(calls) == 1 and result.errors
