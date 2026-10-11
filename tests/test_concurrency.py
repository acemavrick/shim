import asyncio
from importlib import import_module

import httpx

from shim import compile as compiler
from shim.config import Config
from shim.manifest import Action, CheckResult, Extract, HttpBackend, InputSchema, Session
from shim.runtime import http

crawler = import_module("shim.explore.crawl")


def test_crawl_is_bounded_ordered_and_respects_robots(monkeypatch):
    config = Config(crawl_concurrency=3)
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: config)
    active = peak = 0
    fetched = []
    async def fetch(url):
        nonlocal active, peak
        fetched.append(url)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.004 if url.endswith("/1") else 0.001)
            if url.endswith("robots.txt"):
                body = "User-agent: *\nDisallow: /blocked"
            else:
                body = '<title>Page</title>' + ''.join(f'<a href="/{n}">Page</a>' for n in ['1', '2', 'blocked', '3', '4', '5'])
            return http.RawResponse(body, "text/html", 200, url, len(body))
        finally:
            active -= 1
    monkeypatch.setattr(crawler, "fetch_page", fetch)
    result = asyncio.run(crawler.crawl("https://site.test/", max_pages=5))
    assert [p.url for p in result.pages] == ["https://site.test/", *[f"https://site.test/{n}" for n in range(1, 5)]]
    assert peak == 3
    assert "https://site.test/blocked" not in fetched
    assert "https://site.test/5" not in fetched
    assert result.skips[0].reason == "robots-disallowed"


def test_check_writes_and_session_requests_are_barriers_and_names_stay_ordered(monkeypatch):
    active = peak = 0
    finished = []
    def action(name, **kwargs):
        return Action(name=name, description=name, tier=kwargs.pop("tier", "read"),
                      input=InputSchema(), extract=Extract(fields={}),
                      backend=HttpBackend(method=kwargs.pop("method", "GET"), url=f"https://site.test/{name}"), **kwargs)
    actions = [action("first"), action("second"), action("write", tier="reversible", method="POST"),
               action("session", session=Session(carry_cookies=True)), action("last")]
    async def check(action, *args, **kwargs):
        nonlocal active, peak
        if action.name in {"write", "session"}:
            assert active == 0
            assert "first" in finished and "second" in finished
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.003 if action.name == "first" else 0.001)
            finished.append(action.name)
            action.name = "renamed_" + action.name
            action.check = CheckResult(passed=True)
            return action, None
        finally:
            active -= 1
    monkeypatch.setattr(compiler, "check_action", check)
    async def run():
        return [item async for item in compiler._checked_actions(actions, [], allow_writes=True,
                                                                 describer=None, concurrency=3)]
    results = asyncio.run(run())
    assert peak == 2
    assert [name for name, _, _ in results] == ["first", "second", "write", "session", "last"]
    assert finished.index("write") < finished.index("session") < finished.index("last")


def test_pacing_preserves_same_origin_gap_without_blocking_other_sites(monkeypatch):
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: Config(min_request_gap=0.05))
    starts = {}
    async def handle(request):
        starts[str(request.url)] = asyncio.get_running_loop().time()
        return httpx.Response(200, text="ok")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(http, "_get_client", lambda: client)
            await asyncio.gather(http.fetch_page("https://a.test/1"), http.fetch_page("https://a.test/2"),
                                 http.fetch_page("https://b.test/1"))
    asyncio.run(run())
    assert starts["https://a.test/2"] - starts["https://a.test/1"] >= 0.045
    assert starts["https://b.test/1"] < starts["https://a.test/2"]


def test_cancellation_drains_checks(monkeypatch):
    active = 0
    async def check(*args, **kwargs):
        nonlocal active
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    monkeypatch.setattr(compiler, "check_action", check)
    actions = [Action(name=f"a{i}", description="Read", tier="read", input=InputSchema(), extract=Extract(fields={}),
                      backend=HttpBackend(method="GET", url="https://site.test"))
               for i in range(4)]
    async def run():
        async def consume():
            async for _ in compiler._checked_actions(actions, [], allow_writes=False, describer=None, concurrency=2):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.005)
        assert active == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert active == 0
    asyncio.run(run())
