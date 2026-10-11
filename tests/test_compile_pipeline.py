import asyncio
from importlib import import_module

from shim import compile as compiler
from shim.config import Config
from shim.explore.agent import AgentResult
from shim.explore.crawl import CrawledPage, CrawlResult
from shim.manifest import Action, CheckResult, Extract, HttpBackend, InputSchema, Step
from shim.translate.forms import parse_forms


def test_verified_tools_arrive_before_crawl_finishes_and_agent_runs_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, 'SITES_DIR', tmp_path)
    monkeypatch.setattr(import_module('shim.config'), 'config', lambda: Config(anthropic_api_key='test'))
    monkeypatch.setattr(import_module('shim.translate.describe'), 'make_completer', lambda *a, **kw: None)
    async def run():
        agent_started, verified = asyncio.Event(), asyncio.Event()
        events = []
        crawl_finished = False
        async def crawl(url, on_page, **kwargs):
            nonlocal crawl_finished
            await asyncio.wait_for(agent_started.wait(), 1)
            html = '<main><form action="/list"><input type="hidden" name="kind" value="book"><button>List books</button></form></main>'
            page = CrawledPage(url, html)
            result = CrawlResult(pages=[page], forms=parse_forms(html, url))
            await on_page(page, result)
            await asyncio.wait_for(verified.wait(), 1)
            crawl_finished = True
            return result
        class Explorer:
            def __init__(self, *args, **kwargs):
                pass
            async def run(self, url):
                agent_started.set()
                await verified.wait()
                return AgentResult(procedure=[Step(note='Use the listed tools')])
        async def check(action, *args, **kwargs):
            await asyncio.sleep(0)
            action.check = CheckResult(passed=True, status=200, items_found=1)
            return action, None
        async def emit(event):
            events.append(event)
            if event['type'] == 'action' and not crawl_finished:
                verified.set()
        monkeypatch.setattr(compiler, 'crawl', crawl)
        monkeypatch.setattr(compiler, 'check_action', check)
        monkeypatch.setattr(import_module('shim.explore.agent'), 'AgentExplorer', Explorer)
        manifest = await compiler.compile_site('https://site.test/', agent=True, intent='Read', emit=emit)
        assert verified.is_set() and len(manifest.actions) == 1
        kinds = [e['type'] for e in events]
        assert kinds.index('tool_candidate') < kinds.index('tool_checking') < kinds.index('action') < kinds.index('done')
        assert len({e['candidate_id'] for e in events if e['type'] == 'action'}) == 1
        assert any(set(e.get('active_stages', [])) >= {'agent', 'explore'} for e in events)
    asyncio.run(run())


def test_checks_refill_before_slowest_predecessor_finishes(monkeypatch):
    async def run():
        third_started = asyncio.Event()
        actions = [Action(name=f'a{i}', description='Read', tier='read', input=InputSchema(),
                          extract=Extract(fields={}), backend=HttpBackend(method='GET', url=f'https://site.test/{i}'))
                   for i in range(3)]
        async def check(action, *args, **kwargs):
            if action.name == 'a0':
                await asyncio.wait_for(third_started.wait(), 1)
            elif action.name == 'a2':
                third_started.set()
            await asyncio.sleep(0)
            return action, None
        monkeypatch.setattr(compiler, 'check_action', check)
        results = [name async for name, _, _ in compiler._checked_actions(
            actions, [], allow_writes=False, describer=None, concurrency=2)]
        assert results == ['a0', 'a1', 'a2']
    asyncio.run(run())


def test_crawl_cancellation_drains_early_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, 'SITES_DIR', tmp_path)
    monkeypatch.setattr(import_module('shim.config'), 'config', lambda: Config())
    async def run():
        checking, cancelled = asyncio.Event(), asyncio.Event()
        async def crawl(url, on_page, **kwargs):
            html = '<form action="/read"><input name="q"><button>Search</button></form>'
            result = CrawlResult(pages=[CrawledPage(url, html)], forms=parse_forms(html, url))
            await on_page(result.pages[0], result)
            await asyncio.Event().wait()
        async def check(*args, **kwargs):
            checking.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        monkeypatch.setattr(compiler, 'crawl', crawl)
        monkeypatch.setattr(compiler, 'check_action', check)
        task = asyncio.create_task(compiler.compile_site('https://site.test/', agent=False))
        await asyncio.wait_for(checking.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert cancelled.is_set()
        assert not (tmp_path / 'site.test' / 'manifest.json').exists()
    asyncio.run(run())


def test_stateful_form_waits_for_earlier_link_check(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, 'SITES_DIR', tmp_path)
    monkeypatch.setattr(import_module('shim.config'), 'config', lambda: Config())

    async def run():
        link_started, release_link, final_check_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        link_active = write_overlapped = False

        async def crawl(url, on_page, **kwargs):
            html = ''.join(f'<a href="/books/{i}">Book {i}</a>' for i in range(1, 5))
            first = CrawledPage(url, html)
            result = CrawlResult(pages=[first])
            await on_page(first, result)
            await asyncio.wait_for(link_started.wait(), 1)
            html = '<form action="/cart" method="post"><input name="item"><button>Add item</button></form>'
            second = CrawledPage(url + 'cart', html)
            result.pages.append(second)
            result.forms.extend(parse_forms(html, second.url))
            await on_page(second, result)
            return result

        async def check(action, *args, **kwargs):
            nonlocal link_active, write_overlapped
            if action.backend.method == 'GET':
                link_active = True
                link_started.set()
                try:
                    await release_link.wait()
                finally:
                    link_active = False
            else:
                write_overlapped = link_active
            action.check = CheckResult(passed=True, status=200, items_found=1)
            return action, None

        async def emit(event):
            if event.get('type') == 'stage' and event.get('stage') == 'check':
                final_check_started.set()

        async def release():
            await final_check_started.wait()
            release_link.set()

        monkeypatch.setattr(compiler, 'crawl', crawl)
        monkeypatch.setattr(compiler, 'check_action', check)
        release_task = asyncio.create_task(release())
        try:
            manifest = await asyncio.wait_for(compiler.compile_site('http://localhost/', agent=False, emit=emit), 1)
            assert not write_overlapped
            assert len(manifest.actions) == 2
        finally:
            release_task.cancel()
            await asyncio.gather(release_task, return_exceptions=True)

    asyncio.run(run())


def test_superseded_early_check_cannot_publish_after_done(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, 'SITES_DIR', tmp_path)
    monkeypatch.setattr(import_module('shim.config'), 'config', lambda: Config())

    async def run():
        old_started, release_old = asyncio.Event(), asyncio.Event()
        events = []

        async def crawl(url, on_page, **kwargs):
            html = ''.join(f'<a href="/books/{i}">Book {i}</a>' for i in range(1, 5))
            first = CrawledPage(url, html)
            result = CrawlResult(pages=[first])
            await on_page(first, result)
            await asyncio.wait_for(old_started.wait(), 1)
            second = CrawledPage(url + 'next', '<a href="/books/another-book-2">Another</a>')
            result.pages.append(second)
            await on_page(second, result)
            return result

        async def check(action, *args, **kwargs):
            if '{id}' in action.backend.url:
                old_started.set()
                await release_old.wait()
            action.check = CheckResult(passed=True, status=200, items_found=1)
            return action, None

        async def emit(event):
            events.append(event)
            if event.get('type') == 'done':
                release_old.set()

        monkeypatch.setattr(compiler, 'crawl', crawl)
        monkeypatch.setattr(compiler, 'check_action', check)
        manifest = await asyncio.wait_for(compiler.compile_site('https://site.test/', agent=False, emit=emit), 1)
        assert len(manifest.actions) == manifest.coverage.actions_shipped == 1
        assert '{slug}' in manifest.actions[0].backend.url
        assert events[-1]['type'] == 'done'
        removed = next(i for i, event in enumerate(events)
                       if event['type'] == 'action_failed' and event.get('candidate_id') == 'tool-1')
        assert not any(event['type'] == 'action' and event.get('candidate_id') == 'tool-1'
                       for event in events[removed + 1:])

    asyncio.run(run())


def test_model_names_stay_unique_and_failed_candidates_do_not_reserve_names(tmp_path, monkeypatch):
    from shim.manifest import Skip
    monkeypatch.setattr(compiler, 'SITES_DIR', tmp_path)
    monkeypatch.setattr(import_module('shim.config'), 'config', lambda: Config())

    async def crawl(url, on_page, **kwargs):
        html = ''.join(f'<form action="/{name}"><input name="q"><button>{name}</button></form>'
                       for name in ['failed', 'one', 'two', 'three'])
        page = CrawledPage(url, html)
        result = CrawlResult(pages=[page], forms=parse_forms(html, url))
        await on_page(page, result)
        return result

    async def check(action, *args, **kwargs):
        action.name = 'search_2' if action.backend.url.endswith('/three') else 'search'
        if action.backend.url.endswith('/failed'):
            action.check = CheckResult(passed=False, error='No records')
            return action, Skip(what=action.name, reason='check-failed', detail='No records')
        action.check = CheckResult(passed=True, status=200, items_found=1)
        return action, None

    monkeypatch.setattr(compiler, 'crawl', crawl)
    monkeypatch.setattr(compiler, 'check_action', check)
    events = []
    manifest = asyncio.run(compiler.compile_site('https://site.test/', agent=False, emit=events.append))
    names = [action.name for action in manifest.actions]
    assert names == ['search', 'search_2', 'search_2_2']
    assert manifest.coverage.actions_generated == 4 and manifest.coverage.actions_shipped == len(set(names)) == 3
    assert len([skip for skip in manifest.coverage.skipped if skip.reason == 'check-failed']) == 1
    assert events[-1]['type'] == 'done' and events[-1]['shipped'] == 3
