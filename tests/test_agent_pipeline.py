import asyncio
import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from shim.explore.agent import AgentExplorer, AgentResult, Observed, _StepAction
from shim.explore.crawl import CrawledPage
from shim.manifest import Action, FromInput

agent = import_module("shim.explore.agent")


def test_write_down_emits_each_evidenced_definition_before_constructing_next(monkeypatch):
    made = []
    events = []
    original = agent._tool_to_action
    result = AgentResult(pages=[CrawledPage("https://site.test/", "<main>Page</main>")], observed=[
        Observed(0, "GET", f"https://site.test/{name}", "document", 200, "text/html")
        for name in ["first", "second"]])

    def construct(spec, start):
        made.append(spec.name)
        return original(spec, start)

    async def on_event(event):
        events.append(event)
        if event["type"] == "agent_tool":
            assert len(made) == len([e for e in events if e["type"] == "agent_tool"])
            assert len(result.tools) == len(made)
            assert "candidate_id" not in event
            assert "check" not in event["definition"]

    async def complete(system, prompt):
        return json.dumps({"tools": [
            {"name": name, "description": "Read page", "tier": "read", "method": "GET",
             "url": f"https://site.test/{name}", "extract": {"fields": {"title": "h1"}}}
            for name in ["first", "invented", "second"]]})

    monkeypatch.setattr(agent, "_tool_to_action", construct)
    asyncio.run(AgentExplorer(complete, "Learn site", on_event=on_event)._write_down(result))
    definitions = [e["definition"] for e in events if e["type"] == "agent_tool"]
    assert [d["name"] for d in definitions] == ["first", "second"]
    assert [d["backend"]["url"] for d in definitions] == ["https://site.test/first", "https://site.test/second"]
    assert any("invented" in error for error in result.errors)


def test_agent_tool_event_roundtrips_real_input_and_constant_sources():
    parsed = []

    async def on_event(event):
        if event['type'] == 'agent_tool':
            # This is the same strict revalidation the compiler performs.
            parsed.append(Action.model_validate(event['definition']))

    async def complete(system, prompt):
        return json.dumps({'tools': [{'name': 'search_books', 'description': 'Search books', 'tier': 'read',
            'method': 'GET', 'url': 'https://site.test/search',
            'inputs': {'category': {'wire': 'category', 'examples': ['science', 'history']}},
            'constants': {'lang': 'en'}, 'extract': {'kind': 'json', 'items': 'books', 'fields': {'title': 'title'}}}]})

    result = AgentResult(observed=[Observed(0, 'GET', f'https://site.test/search?category={category}&lang=en',
                                           'fetch', 200, 'application/json')
                                  for category in ['science', 'history']])
    asyncio.run(AgentExplorer(complete, 'Learn', on_event=on_event)._write_down(result))
    assert len(result.tools) == len(parsed) == 1 and not result.errors
    assert isinstance(parsed[0].backend.fields['category'], FromInput)
    assert parsed[0].backend.fields['category'].name == 'category'
    assert parsed[0].backend.fields['lang'].value == 'en'


def test_bad_tool_entries_do_not_erase_valid_tools_or_collected_pages():
    events, prompts = [], []
    good = {'name': 'read_books', 'description': 'Read books', 'tier': 'read', 'method': 'GET',
            'url': 'https://site.test/books', 'extract': {'fields': {'title': 'h1'}}}
    invalid_extract = {**good, 'name': 'bad_extract', 'extract': {'kind': 'private-invalid-value'}}
    invalid_input = {**good, 'name': 'bad_input', 'inputs': {'q': {'description': ['private-invalid-value']}}}
    second = {**good, 'name': 'read_other', 'url': 'https://site.test/other'}

    async def complete(system, prompt):
        prompts.append(prompt)
        return json.dumps({'tools': [good, invalid_extract, invalid_input, None, second],
                           'summary': 'Two reusable reads', 'notes': ['A useful limitation']})

    pages = [CrawledPage('https://site.test/books', '<h1>Actual books</h1>')]
    result = AgentResult(pages=list(pages), observed=[
        Observed(0, 'GET', f'https://site.test/{path}', 'document', 200, 'text/html')
        for path in ['books', 'other']])
    explorer = AgentExplorer(complete, 'Learn', on_event=events.append)
    asyncio.run(explorer._write_down(result))
    assert explorer.model_calls == len(prompts) == 1
    assert [tool.name for tool in result.tools] == ['read_books', 'read_other']
    assert result.pages == pages and result.summary == 'Two reusable reads' and result.notes == ['A useful limitation']
    assert len(result.errors) == 3
    assert 'private-invalid-value' not in json.dumps(result.errors)
    assert len([event for event in events if event['type'] == 'agent_tool_rejected']) == 3
    assert not any(event['type'] == 'agent_write_down_failed' for event in events)


def test_rendered_page_discovery_emits_actual_captured_evidence():
    events = []
    result = AgentResult()
    html = '<main><h1>Rendered</h1><a href="/books/42">Book</a></main>'

    async def content():
        return html

    async def title():
        return 'Rendered title'

    async def on_event(event):
        assert len(result.pages) == 1
        events.append(event)

    page = SimpleNamespace(url='https://site.test/books', content=content, title=title)
    asyncio.run(AgentExplorer(None, 'Learn', on_event=on_event)._capture_page(page, result))
    assert events == [{'type': 'agent_page', 'url': page.url, 'html': html, 'title': 'Rendered title'}]


def test_agent_step_progress_includes_configured_budget_for_actions_and_done(monkeypatch):
    events = []
    replies = iter(['{"action":"inspect","tool":"network"}', '{"action":"done","summary":"Read evidence"}', '{}'])

    async def complete(system, prompt):
        return next(replies)

    async def noop(*args, **kwargs):
        pass

    page = SimpleNamespace(url='https://site.test/', on=lambda *args: None, goto=noop, wait_for_load_state=noop)

    async def new_page():
        return page

    context = SimpleNamespace(route=noop, new_page=new_page)

    async def new_context(**kwargs):
        return context

    browser = SimpleNamespace(new_context=new_context, close=noop)

    async def launch(**kwargs):
        return browser

    async def read_state(*args, **kwargs):
        return '- heading Page', []

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(import_module('playwright.async_api'), 'async_playwright', Playwright)
    monkeypatch.setattr(AgentExplorer, '_read_state', read_state)
    result = asyncio.run(AgentExplorer(complete, 'Learn', step_budget=23, on_event=events.append).run(page.url))
    progress = [event for event in events if event['type'] == 'agent_step']
    assert result.steps_taken == 2
    assert [(event['step'], event['step_budget'], event['action']) for event in progress] == [(1, 23, 'inspect'), (2, 23, 'done')]


def test_state_reads_overlap_and_document_capture_happens_once_per_page():
    async def run():
        document_started = asyncio.Event()
        snapshot_started = asyncio.Event()
        html_started = asyncio.Event()
        title_started = asyncio.Event()
        captured = []
        disposed = []

        async def get(url, **kwargs):
            captured.append(url)
            document_started.set()
            await snapshot_started.wait()
            async def text():
                return "<h1>Page</h1>"
            async def dispose():
                disposed.append(url)
            return SimpleNamespace(url=url, status=200, headers={"content-type": "text/html"},
                                   text=text, dispose=dispose)

        async def snapshot():
            snapshot_started.set()
            await document_started.wait()
            return "- heading Page"

        async def anchors(script):
            return [{"name": "Other", "url": "https://site.test/other"}]

        async def content():
            html_started.set()
            await title_started.wait()
            return "<h1>Page</h1>"

        async def title():
            title_started.set()
            await html_started.wait()
            return "Page"

        page = SimpleNamespace(url="https://site.test/", content=content, title=title,
            locator=lambda selector: SimpleNamespace(aria_snapshot=snapshot, evaluate_all=anchors))
        context = SimpleNamespace(request=SimpleNamespace(get=get))
        result = AgentResult()
        explorer = AgentExplorer(None, "Learn")
        snapshot_text, links = await asyncio.wait_for(explorer._read_state(page, context, result, 0, True), 1)
        assert snapshot_text == "- heading Page"
        assert links[0]["name"] == "Other"
        assert result.pages[0].title == "Page"
        assert result.observed[0].sample == "<h1>Page</h1>"
        await explorer._read_state(page, context, result, 1, False)
        assert captured == disposed == ["https://site.test/"]
        assert len(result.pages) == 1

    asyncio.run(run())


def test_state_read_cancellation_drains_all_parallel_reads():
    async def run():
        active = 0
        cancelled = 0
        all_started = asyncio.Event()

        async def hang(*args, **kwargs):
            nonlocal active, cancelled
            active += 1
            if active == 5:
                all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        page = SimpleNamespace(url="https://site.test/", content=hang, title=hang,
            locator=lambda selector: SimpleNamespace(aria_snapshot=hang, evaluate_all=hang))
        context = SimpleNamespace(request=SimpleNamespace(get=hang))
        explorer = AgentExplorer(None, "Learn")
        task = asyncio.create_task(explorer._read_state(page, context, AgentResult(), 0, True))
        await asyncio.wait_for(all_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert cancelled == 5

    asyncio.run(run())


@pytest.mark.parametrize("action", ["fill", "scroll", "click", "goto"])
def test_browser_actions_still_settle_before_reading_next_state(action):
    waits = []
    async def noop(*args, **kwargs):
        pass
    async def count():
        return 1
    async def wait_for_load_state(*args, **kwargs):
        waits.append((args, kwargs))
    locator = SimpleNamespace(first=None, count=count, fill=noop, click=noop)
    locator.first = locator
    page = SimpleNamespace(goto=noop, mouse=SimpleNamespace(wheel=noop),
                           get_by_role=lambda *args, **kwargs: locator,
                           wait_for_load_state=wait_for_load_state)
    act = _StepAction(action=action, role="textbox", name="Search", text="Book", url="https://site.test/")
    assert asyncio.run(AgentExplorer(None, "Learn")._do(page, act)) is None
    assert len(waits) == 1
    assert waits[0][0] == ("networkidle",)


@pytest.mark.parametrize("cancel", [False, True])
def test_browser_closes_when_initial_navigation_fails_or_is_cancelled(monkeypatch, cancel):
    playwright = import_module("playwright.async_api")
    closed = []

    async def run():
        navigating = asyncio.Event()
        async def noop(*args, **kwargs):
            pass
        async def goto(*args, **kwargs):
            navigating.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("navigation failed")
        page = SimpleNamespace(on=lambda *args: None, goto=goto)
        async def new_page():
            return page
        context = SimpleNamespace(route=noop, new_page=new_page)
        async def new_context(**kwargs):
            return context
        async def close():
            closed.append(True)
        browser = SimpleNamespace(new_context=new_context, close=close)
        async def launch(**kwargs):
            return browser
        class Playwright:
            async def __aenter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch=launch))
            async def __aexit__(self, *args):
                pass
        monkeypatch.setattr(playwright, "async_playwright", Playwright)
        task = asyncio.create_task(AgentExplorer(None, "Learn").run("https://site.test/"))
        await asyncio.wait_for(navigating.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        assert closed == [True]

    asyncio.run(run())
