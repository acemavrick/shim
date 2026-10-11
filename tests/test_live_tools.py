import asyncio
import json

import pytest

from shim import web
from shim.manifest import Action, CheckResult, Extract, HttpBackend, InputSchema


def test_durable_version_survives_expired_live_registry(tmp_path, monkeypatch):
    from shim import compile as compiler
    from shim.history import new_run, publish_run
    from shim.manifest import Manifest
    monkeypatch.setattr(compiler, "SITES_DIR", tmp_path)
    monkeypatch.setattr(web, "SITES_DIR", tmp_path)
    monkeypatch.setattr(web, "LIVE_RUNS", {})
    action = Action(name="read", description="Read", tier="read", input=InputSchema(),
                    backend=HttpBackend(method="GET", url="https://site.test/read"),
                    extract=Extract(fields={"title": "h1"}), check=CheckResult(passed=True))
    async def compile_site(url, emit, **kwargs):
        version, directory = new_run("site.test")
        await emit({"type": "run", "run_id": version})
        await emit({"type": "action", "candidate_id": "tool-1", "name": "read", "definition": action.model_dump()})
        await publish_run(Manifest(site=url, host="site.test", compiled_at="now", run_id=version, actions=[action]), directory)
        await emit({"type": "done", "run_id": version})
    async def invoke(checked, arguments):
        from shim.runtime.http import CallResult
        return CallResult(records=[{"title": "Saved"}], items_found=1, status=200, bytes=5, url=checked.backend.url)
    monkeypatch.setattr(web, "compile_site", compile_site)
    monkeypatch.setattr(web, "invoke_action", invoke)
    async def run():
        response = await web.compile_stream("https://site.test/", max_pages=1, agent=False)
        events = [json.loads(e.removeprefix("data: ").strip()) async for e in response.body_iterator]
        version = events[0]["run_id"]
        live_id = events[0]["live_id"]
        assert version != live_id and all(e["run_id"] == version for e in events)
        assert (await web.call(web.CallRequest(host="site.test", action="read", run_id=version,
                    live_id=live_id, candidate_id="tool-1")))["records"] == [{"title": "Saved"}]
        web.LIVE_RUNS.clear()
        assert (await web.call(web.CallRequest(host="site.test", action="read", run_id=version)))["records"] == [{"title": "Saved"}]
        assert (await web.manifest("site.test", version))["run_id"] == version
        assert (await web.agent_file("site.test", "llms.txt", version)).path.exists()
    asyncio.run(run())


def test_only_verified_run_tools_are_callable_before_done_and_runs_stay_isolated(monkeypatch):
    async def run():
        action = Action(name='read', description='Read fresh', tier='read', input=InputSchema(),
                        extract=Extract(fields={'title': 'h1'}),
                        backend=HttpBackend(method='GET', url='https://site.test/read'))
        finished = False
        release_check = asyncio.Event()
        async def compile_site(url, emit, **kwargs):
            nonlocal finished
            await emit({'type': 'tool_candidate', 'candidate_id': 'tool-1', 'definition': action.model_dump(), 'name': action.name})
            await release_check.wait()
            action.check = CheckResult(passed=True, status=200, items_found=1)
            await emit({'type': 'action', 'candidate_id': 'tool-1', 'definition': action.model_dump(), 'name': action.name})
            await asyncio.Event().wait()
            finished = True
        async def invoke(checked, arguments):
            assert not finished
            assert checked.check.passed
            return type('Result', (), {'status': 200, 'items_found': 1, 'bytes': 4, 'records': [{'title': 'Fresh'}], 'url': checked.backend.url, 'blocked': False})()
        monkeypatch.setattr(web, 'compile_site', compile_site)
        monkeypatch.setattr(web, 'invoke_action', invoke)
        response = await web.compile_stream('https://site.test/', max_pages=1, agent=False)
        stream = response.body_iterator
        first = json.loads((await anext(stream)).removeprefix('data: ').strip())
        req = web.CallRequest(host='site.test', action='read', run_id=first['run_id'], candidate_id='tool-1')
        with pytest.raises(web.HTTPException) as unverified:
            await web.call(req)
        assert unverified.value.status_code == 404
        release_check.set()
        second = json.loads((await anext(stream)).removeprefix('data: ').strip())
        assert first['run_id'] == second['run_id'] and second['type'] == 'action'
        result = await web.call(req)
        assert result['records'] == [{'title': 'Fresh'}]
        with pytest.raises(web.HTTPException) as wrong_site:
            await web.call(req.model_copy(update={'host': 'other.test'}))
        assert wrong_site.value.status_code == 404
        with pytest.raises(web.HTTPException) as unknown_run:
            await web.call(req.model_copy(update={'run_id': 'missing'}))
        assert unknown_run.value.status_code == 410
        await stream.aclose()
        assert web.LIVE_RUNS[first['run_id']]['finished'] is not None
    asyncio.run(run())


def test_ambiguous_live_name_requires_candidate_id_and_calls_exact_candidate(monkeypatch):
    actions = {
        f'tool-{index}': Action(name='read', description=f'Read {index}', tier='read', input=InputSchema(),
                              extract=Extract(fields={'title': 'h1'}), check=CheckResult(passed=True),
                              backend=HttpBackend(method='GET', url=f'https://site.test/read/{index}'))
        for index in (1, 2)
    }
    monkeypatch.setattr(web, 'LIVE_RUNS', {'collision': {'host': 'site.test', 'actions': actions, 'finished': None}})
    invoked = []

    async def invoke(action, arguments):
        invoked.append(action.backend.url)
        return type('Result', (), {'status': 200, 'items_found': 1, 'bytes': 4, 'records': [],
                                 'url': action.backend.url, 'blocked': False})()

    monkeypatch.setattr(web, 'invoke_action', invoke)

    async def run():
        req = web.CallRequest(host='site.test', action='read', run_id='collision')
        with pytest.raises(web.HTTPException) as ambiguous:
            await web.call(req)
        assert ambiguous.value.status_code == 409 and 'candidate_id' in ambiguous.value.detail
        assert not invoked
        result = await web.call(req.model_copy(update={'candidate_id': 'tool-2'}))
        assert result['source'] == 'https://site.test/read/2'
        assert invoked == ['https://site.test/read/2']

    asyncio.run(run())
