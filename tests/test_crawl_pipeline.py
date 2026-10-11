import asyncio
from importlib import import_module

import pytest

from shim.config import Config
from shim.runtime.http import RawResponse

crawler = import_module("shim.explore.crawl")
ORIGIN = "https://site.test"


def configure(monkeypatch, concurrency=2):
    cfg = Config(respect_robots=False, crawl_concurrency=concurrency)
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: cfg)


def response(url, links=(), status=200, content_type="text/html"):
    body = "<title>Page</title>" + "".join(f'<a href="/{link}">Link</a>' for link in links)
    return RawResponse(body, content_type, status, url, len(body))


def test_pipeline_refills_before_slow_sibling_and_keeps_bfs_budget(monkeypatch):
    configure(monkeypatch)
    fetched = []
    active = peak = 0

    async def run():
        b_started = asyncio.Event()
        c_started = asyncio.Event()

        async def fetch(url):
            nonlocal active, peak
            fetched.append(url)
            active += 1
            peak = max(peak, active)
            try:
                path = url.removeprefix(ORIGIN + "/")
                if not path:
                    return response(url, ["a", "b", "c"])
                if path == "a":
                    await b_started.wait()
                elif path == "b":
                    b_started.set()
                    # A batch barrier deadlocks here: c must start while b waits.
                    await c_started.wait()
                elif path == "c":
                    c_started.set()
                return response(url, [path + "-child"])
            finally:
                active -= 1

        monkeypatch.setattr(crawler, "fetch_page", fetch)
        return await asyncio.wait_for(crawler.crawl(ORIGIN + "/", max_pages=6), 1)

    result = asyncio.run(run())
    assert [page.url for page in result.pages] == [
        ORIGIN + "/" + path for path in ["", "a", "b", "c", "a-child", "b-child"]
    ]
    assert len(fetched) == 6
    assert ORIGIN + "/c-child" not in fetched
    assert peak == 2
    assert active == 0


def test_failures_release_budget_without_changing_discovery_order(monkeypatch):
    configure(monkeypatch, concurrency=3)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        path = url.removeprefix(ORIGIN + "/")
        if not path:
            return response(url, ["error", "missing", "data", "ok", "extra"])
        if path == "error":
            raise OSError("offline")
        if path == "missing":
            return response(url, status=404)
        if path == "data":
            return response(url, content_type="application/json")
        return response(url)

    monkeypatch.setattr(crawler, "fetch_page", fetch)
    result = asyncio.run(crawler.crawl(ORIGIN + "/", max_pages=2))
    assert [page.url for page in result.pages] == [ORIGIN + "/", ORIGIN + "/ok"]
    assert result.pages_failed == 3
    assert result.skips[0].reason == "fetch-error"
    assert ORIGIN + "/extra" not in fetched


def test_out_of_order_block_stops_refill_and_cancels_remaining_download(monkeypatch):
    configure(monkeypatch, concurrency=3)
    fetched = []
    active = 0
    cancelled = []

    async def run():
        hang_started = asyncio.Event()
        suffix_cancelled = asyncio.Event()

        async def fetch(url):
            nonlocal active
            fetched.append(url)
            active += 1
            try:
                path = url.removeprefix(ORIGIN + "/")
                if not path:
                    return response(url, ["a", "blocked", "hang", "extra"])
                if path == "a":
                    # Later requests must be cancelled immediately, even while
                    # this preceding BFS request has not finished.
                    await suffix_cancelled.wait()
                elif path == "blocked":
                    await hang_started.wait()
                    return response(url, status=429)
                elif path == "hang":
                    hang_started.set()
                    await asyncio.Event().wait()
                return response(url)
            except asyncio.CancelledError:
                cancelled.append(url)
                suffix_cancelled.set()
                raise
            finally:
                active -= 1

        monkeypatch.setattr(crawler, "fetch_page", fetch)
        return await asyncio.wait_for(crawler.crawl(ORIGIN + "/", max_pages=8), 1)

    result = asyncio.run(run())
    assert result.blocked
    assert [page.url for page in result.pages] == [ORIGIN + "/", ORIGIN + "/a"]
    assert [skip.reason for skip in result.skips] == ["bot-protection"]
    assert result.pages_failed == 0
    assert ORIGIN + "/extra" not in fetched
    assert cancelled == [ORIGIN + "/hang"]
    assert active == 0


def test_crawl_cancellation_drains_all_downloads(monkeypatch):
    configure(monkeypatch)
    active = 0

    async def run():
        both_started = asyncio.Event()

        async def fetch(url):
            nonlocal active
            if url == ORIGIN + "/":
                return response(url, ["a", "b"])
            active += 1
            if active == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        monkeypatch.setattr(crawler, "fetch_page", fetch)
        task = asyncio.create_task(crawler.crawl(ORIGIN + "/", max_pages=3))
        await asyncio.wait_for(both_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0

    asyncio.run(run())


def test_callback_error_drains_pending_downloads(monkeypatch):
    configure(monkeypatch)
    active = 0

    async def run():
        sibling_started = asyncio.Event()

        async def fetch(url):
            nonlocal active
            if url == ORIGIN + "/":
                return response(url, ["a", "b"])
            active += 1
            try:
                if url.endswith("/a"):
                    await sibling_started.wait()
                    return response(url)
                sibling_started.set()
                await asyncio.Event().wait()
            finally:
                active -= 1

        def on_page(page, result):
            if page.url.endswith("/a"):
                raise ValueError("callback failed")

        monkeypatch.setattr(crawler, "fetch_page", fetch)
        with pytest.raises(ValueError, match="callback failed"):
            await asyncio.wait_for(crawler.crawl(ORIGIN + "/", max_pages=3, on_page=on_page), 1)
        assert active == 0

    asyncio.run(run())


def test_blocked_head_cancels_scheduled_requests_before_they_start(monkeypatch):
    configure(monkeypatch)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        if url == ORIGIN + "/":
            return response(url, ["blocked", "unsent"])
        return response(url, status=403)

    monkeypatch.setattr(crawler, "fetch_page", fetch)
    result = asyncio.run(crawler.crawl(ORIGIN + "/", max_pages=3))
    assert result.blocked
    assert fetched == [ORIGIN + "/", ORIGIN + "/blocked"]
    assert [skip.reason for skip in result.skips] == ["bot-protection"]
    assert result.pages_failed == 0
