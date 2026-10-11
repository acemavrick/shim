import asyncio
import json

from shim.explore.agent import AgentExplorer, AgentResult, Observed
from shim.explore.crawl import CrawledPage


def test_write_down_keeps_variations_and_rejects_invented_endpoints():
    observed = [Observed(step=i, method="GET", url=f"https://shop.test/search?q={query}",
                         resource_type="fetch", status=200, content_type="application/json",
                         sample=json.dumps({"products": [{"name": query}]}))
                for i, query in enumerate(["laptop", "phone"])]
    prompts = []
    async def complete(system, prompt):
        prompts.append(prompt)
        return json.dumps({"tools": [
            {"name": "search_products", "description": "Find products", "tier": "read",
             "method": "GET", "url": "https://shop.test/search", "encoding": "query",
             "inputs": {"query": {"wire": "q", "in": "query", "examples": ["laptop", "phone"]}},
             "extract": {"kind": "json", "items": "products", "fields": {"name": "name"}}},
            {"name": "invented", "description": "No evidence", "tier": "read",
             "method": "GET", "url": "https://shop.test/fake"}], "procedure": []})
    result = AgentResult(observed=observed, pages=[CrawledPage(url="https://shop.test", html="<html></html>")])
    asyncio.run(AgentExplorer(complete, "Find products")._write_down(result))
    assert 'q=laptop' in prompts[0] and 'q=phone' in prompts[0]
    assert [a.name for a in result.tools] == ["search_products"]
    assert result.tools[0].extract.kind == "json"
    assert result.tools[0].extract.items == "products"


def test_agent_input_types_survive_into_replay():
    from shim.explore.agent import _ToolSpec, _tool_to_action
    from shim.runtime.http import resolve_fields
    spec = _ToolSpec(name="get_product", description="Get one product", tier="read", method="POST",
                     url="https://shop.test/get", encoding="json", inputs={
                         "id": {"wire": "id", "in": "body", "type": "integer", "examples": [8, 9]}})
    action = _tool_to_action(spec, "https://shop.test")
    assert action.input.properties["id"].type == "integer"
    assert dict(resolve_fields(action.backend, {"id": 8}))["id"] == 8


def test_step_response_rejects_unknown_browser_actions():
    async def complete(system, prompt):
        return '{"action": "run_javascript", "text": "invented"}'
    assert asyncio.run(AgentExplorer(complete, "Explore")._ask_step("page")) is None


def test_read_identifiers_are_not_generated_or_pinned_as_session_tokens():
    from shim.explore.agent import _ToolSpec, _tool_to_action
    from shim.runtime.http import resolve_fields
    action = _tool_to_action(_ToolSpec(name="get_related_books", description="Find related books",
        tier="read", method="GET", url="https://site.test/related", inputs={
            "id": {"wire": "book_uuid", "examples": ["78f9e812-4b49-4e30-a40d-ca13a759d706"]},
            "slug": {"wire": "book_slug", "examples": ["a-thousand-brains-by-jeff-hawkins"]}}), "https://site.test")
    assert set(action.input.properties) == {"id", "slug"}
    assert dict(resolve_fields(action.backend, {"id": "selected-id", "slug": "selected-slug"})) == {
        "book_uuid": "selected-id", "book_slug": "selected-slug"}


def test_spa_visited_url_gets_direct_response_evidence():
    from types import SimpleNamespace
    async def get(url, **kwargs):
        async def text():
            return "<main>Book details</main>"
        return SimpleNamespace(url=url, status=200, headers={"content-type": "text/html"}, text=text)
    result = AgentResult()
    explorer = AgentExplorer(None, "Learn site")
    asyncio.run(explorer._capture_document(SimpleNamespace(request=SimpleNamespace(get=get)),
                                          "https://site.test/books/one", result, 2))
    assert result.observed[0].url == "https://site.test/books/one"
    assert result.observed[0].sample == "<main>Book details</main>"
    assert result.observed[0].interesting


def test_model_proposes_generic_content_tools_from_static_documents(monkeypatch):
    from shim import check
    from shim.runtime.http import RawResponse
    documents = {
        'https://journal.test/stories/river/': '<main><h1>River</h1><article>River story.</article></main>',
        'https://journal.test/stories/forest/': '<main><h1>Forest</h1><article>Forest story.</article></main>'}
    result = AgentResult(pages=[CrawledPage(url=url, html=html) for url, html in documents.items()],
                         observed=[Observed(step=i, method='GET', url=url, resource_type='document', status=200,
                                            content_type='text/html', sample=html)
                                   for i, (url, html) in enumerate(documents.items())])
    async def model(system, prompt):
        return json.dumps({'tools': [{'name': 'read_story', 'description': 'Read one story', 'tier': 'read',
            'method': 'GET', 'url': 'https://journal.test/stories/{slug}',
            'inputs': {'slug': {'in': 'path', 'examples': ['river', 'forest']}},
            'extract': {'kind': 'css', 'fields': {'title': 'h1', 'content': 'article'}}}], 'procedure': []})
    asyncio.run(AgentExplorer(model, 'Learn this journal')._write_down(result))
    assert [tool.name for tool in result.tools] == ['read_story']
    async def send(backend, args):
        url = backend.url.replace('{slug}', args['slug']) + '/'
        return RawResponse(body=documents[url], url=url, status=200, content_type='text/html', bytes=len(documents[url]))
    monkeypatch.setattr(check, 'send_http', send)
    async def describe(*args):
        from shim.translate.describe import Proposal
        return Proposal(extract=result.tools[0].extract)
    verified, skip = asyncio.run(check.check_action(result.tools[0], ['river', 'forest'], describer=describe))
    assert skip is None and verified.check.passed and verified.check.items_found == 1


def test_invalid_write_down_is_reported_instead_of_silent_empty_success():
    async def model(system, prompt):
        return '{"tools": ['
    events = []
    result = AgentResult()
    asyncio.run(AgentExplorer(model, 'Learn site', on_event=events.append)._write_down(result))
    assert result.errors
    assert any(event['type'] == 'agent_write_down_failed' for event in events)


def test_truncated_write_down_retries_with_a_smaller_interface():
    prompts = []
    async def model(system, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ValueError("model response exceeded the output token budget")
        return '{"tools":[],"notes":["Download requires browser handling."]}'
    result = AgentResult()
    asyncio.run(AgentExplorer(model, "Learn site")._write_down(result))
    assert "at most 4 tools" in prompts[1]
    assert result.notes == ["Download requires browser handling."]
    assert not result.errors
