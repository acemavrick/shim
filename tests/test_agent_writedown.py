import asyncio

import json

from shim.explore.agent import AgentExplorer, AgentResult, Observed, _ToolSpec, _tool_to_action
from shim.manifest import HttpBackend, FromConst, FromGenerate
from shim.runtime import http as rt


def _spec(**kw):
    base = dict(name="x", description="d", tier="read", method="POST", url="https://x.test/api/view",
                encoding="json", inputs={}, constants={})
    base.update(kw)
    return _ToolSpec(**base)


def test_post_read_accepted_only_with_read_hint():
    assert _tool_to_action(_spec(name="get_product_details", inputs={"id": {"wire": "id", "in": "body", "examples": ["8", "9"]}}), "").tier == "read"
    assert _tool_to_action(_spec(name="add_to_cart", url="https://x.test/api/addtocart",
                                 inputs={"id": {"wire": "id", "in": "body", "examples": ["8", "9"]}}), "").tier == "reversible"


def test_generation_is_explicit_and_session_tokens_are_not_pinned():
    a = _tool_to_action(_spec(name="add_to_cart", tier="reversible", inputs={
        "id": {"wire": "id", "in": "body", "source": "generate", "generate": "uuid"},
        "prod_id": {"wire": "prod_id", "in": "body", "examples": ["8", "3"]},
    }, constants={"flag": False}), "")
    assert list(a.input.properties) == ["prod_id"], "only prod_id is the agent's to choose"
    assert isinstance(a.backend.fields["id"], FromGenerate)
    assert a.backend.fields["flag"].value is False, "typed constant survives"
    assert _tool_to_action(_spec(inputs={"cookie": {"examples": ["session-cookie"]}}), "") is None


def test_generate_and_typed_json_body(monkeypatch):
    seen = {}

    async def fake(method, url, **kw):
        seen.update(kw)
        return rt.RawResponse(body="", content_type="application/json", status=200, url=url, bytes=0)

    monkeypatch.setattr(rt, "_polite_fetch", fake)
    backend = HttpBackend(method="POST", url="https://x.test/api/addtocart", encoding="json",
                          fields={"id": FromGenerate(kind="uuid"), "prod_id": FromConst(value=8),
                                  "flag": FromConst(value=False)})
    asyncio.run(rt.send_http(backend, {}))
    import json
    body = json.loads(seen["content"])
    assert body["prod_id"] == 8 and body["flag"] is False and len(body["id"]) == 36
    assert seen["headers"]["origin"] == "https://x.test"


# ----------------------------------------------- evidence spans the whole session

def _html(step, url, **extra):
    return Observed(step=step, method="GET", url=url, resource_type="document", status=200,
                    content_type="text/html", sample="<html><body><h1>x</h1></body></html>", **extra)


def _completer(tools):
    async def complete(system, prompt):
        if "BRIEFING PASS" in prompt:
            return json.dumps({"site_description": "A portfolio", "tools": []})
        return json.dumps({"site_description": "A portfolio", "tools": tools})
    return complete


def test_examples_observed_in_another_batch_are_still_observed():
    # Four distinct paths make two write-down batches; the model lists every
    # slug it saw in the session, not only the ones in the batch it is shown.
    observed = [_html(1, "https://x.test/"), _html(2, "https://x.test/projects/a/"),
                _html(3, "https://x.test/projects/b/"), _html(4, "https://x.test/projects/c/")]
    tool = {"name": "get_project", "description": "Fetch a project", "tier": "read", "method": "GET",
            "url": "https://x.test/projects/{slug}/",
            "inputs": {"slug": {"in": "path", "type": "string", "examples": ["a", "b", "c"]}}}
    result = AgentResult(observed=observed)
    asyncio.run(AgentExplorer(_completer([tool]), "Learn")._write_down(result))
    assert [a.name for a in result.tools] == ["get_project"]
    assert not result.errors


def test_url_observed_only_as_redirect_resolves_to_its_target():
    observed = [
        Observed(step=1, method="GET", url="https://x.test/projects", resource_type="document",
                 status=301, content_type="text/plain", location="/"),
        _html(1, "https://x.test/"),
    ]
    tool = {"name": "list_projects", "description": "List projects", "tier": "read", "method": "GET",
            "url": "https://x.test/projects", "extract": {"kind": "css", "items": "h1", "fields": {"t": ""}}}
    result = AgentResult(observed=observed)
    asyncio.run(AgentExplorer(_completer([tool]), "Learn")._write_down(result))
    assert [a.backend.url for a in result.tools] == ["https://x.test/"]
    assert not result.errors


def test_browser_recipe_start_page_is_checked_against_the_whole_session():
    from shim.explore.crawl import CrawledPage
    recipe = [{"action": "fill", "role": "textbox", "name": "Search"},
              {"action": "press", "role": "textbox", "name": "Search", "text": "Enter"}]
    tool = {"name": "search_site", "description": "Search", "tier": "read",
            "backend": {"kind": "browser", "start_url": "https://x.test/", "steps": [
                {"action": "fill", "role": "textbox", "name": "Search", "text": "{query}"},
                {"action": "press", "role": "textbox", "name": "Search", "text": "Enter"}]},
            "inputs": {"query": {"type": "string", "examples": ["a", "b"]}},
            "extract": {"kind": "css", "items": "li", "fields": {"t": ""}}}
    session = AgentResult(observed=[_html(1, "https://x.test/"), _html(2, "https://x.test/c/")],
                          pages=[CrawledPage(url="https://x.test/", html="<html/>"),
                                 CrawledPage(url="https://x.test/c/", html="<html/>")], recipe=recipe)
    # The batch the model is shown holds only /c/; the start page was visited earlier.
    partial = AgentResult(observed=[_html(2, "https://x.test/c/")],
                          pages=[CrawledPage(url="https://x.test/c/", html="<html/>")], recipe=recipe)
    asyncio.run(AgentExplorer(_completer([tool]), "Learn")._write_down_batch(partial, session=session))
    assert [a.name for a in partial.tools] == ["search_site"]
    assert not partial.errors


def test_api_keys_are_credentials_too():
    from shim.explore.writedown import validate_inputs
    assert "apikey" in validate_inputs(_spec(method="GET", constants={"apikey": "SFX9YPYY9PPXCU9KH"}), [])
    assert _tool_to_action(_spec(method="GET", inputs={"api_key": {"wire": "api_key"}}), "https://x.test/") is None


def test_unobserved_examples_are_pruned_not_fatal():
    from shim.explore.writedown import validate_inputs
    seen = [Observed(step=1, method="POST", url="https://x.test/api/view", resource_type="fetch", status=200,
                     content_type="application/json", post_data='{"id":"8"}')]
    tool = _spec(inputs={"id": {"wire": "id", "examples": ["8", "9"]}})
    assert validate_inputs(tool, seen) is None
    assert tool.inputs["id"]["examples"] == ["8"]
    none_seen = _spec(inputs={"id": {"wire": "id", "examples": ["7", "9"]}})
    assert "unobserved" in validate_inputs(none_seen, seen)
