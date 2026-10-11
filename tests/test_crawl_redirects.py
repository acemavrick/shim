import asyncio
from importlib import import_module

import pytest

from shim.config import Config
from shim.runtime.http import RawResponse

crawler = import_module("shim.explore.crawl")


def setup(monkeypatch, fetch, respect_robots=True):
    cfg = Config(respect_robots=respect_robots, crawl_concurrency=2)
    monkeypatch.setattr(import_module("shim.config"), "config", lambda: cfg)
    monkeypatch.setattr(crawler, "fetch_page", fetch)


def response(url, html):
    return RawResponse(html, "text/html", 200, url, len(html))


@pytest.mark.parametrize("start,canonical", [
    ("https://site.test", "https://www.site.test/"),
    ("https://www.site.test/", "https://site.test/"),
    ("http://site.test/", "https://www.site.test/"),
])
def test_root_canonical_redirect_discovers_final_host_and_uses_its_robots(monkeypatch, start, canonical):
    fetched = []
    source_origin = start.rstrip("/")
    final_origin = canonical.rstrip("/")

    async def fetch(url):
        fetched.append(url)
        if url == source_origin + "/robots.txt":
            return response(url, "User-agent: *\nAllow: /")
        if url == final_origin + "/robots.txt":
            return response(url, "User-agent: *\nDisallow: /blocked")
        if url == start:
            return response(canonical, '<title>Home</title><a href="/first">First</a>'
                '<a href="/blocked">Blocked</a><a href="/second">Second</a>'
                f'<a href="{canonical}">Home</a><a href="{source_origin}/old">Old host</a>'
                '<a href="https://unrelated.test/outside">External</a>')
        await asyncio.sleep(.004 if url.endswith("/first") else .001)
        return response(url, "<title>Child</title>")

    setup(monkeypatch, fetch)
    result = asyncio.run(crawler.crawl(start, max_pages=4))
    assert [page.url for page in result.pages] == [canonical, final_origin + "/first", final_origin + "/second"]
    assert fetched[:3] == [source_origin + "/robots.txt", start, final_origin + "/robots.txt"]
    assert set(fetched[3:]) == {final_origin + "/first", final_origin + "/second"}
    assert [skip.reason for skip in result.skips] == ["robots-disallowed"]
    assert result.skips[0].what == final_origin + "/blocked"


def test_final_robots_disallowed_root_is_not_consumed_or_explored(monkeypatch):
    fetched = []
    async def fetch(url):
        fetched.append(url)
        if url == "https://site.test/robots.txt":
            return response(url, "User-agent: *\nAllow: /")
        if url == "https://www.site.test/robots.txt":
            return response(url, "User-agent: *\nDisallow: /")
        return response("https://www.site.test/", '<a href="/child">Child</a>')
    setup(monkeypatch, fetch)
    result = asyncio.run(crawler.crawl("https://site.test", max_pages=3))
    assert not result.pages
    assert [skip.reason for skip in result.skips] == ["robots-disallowed"]
    assert result.skips[0].what == "https://www.site.test/"
    assert fetched == ["https://site.test/robots.txt", "https://site.test", "https://www.site.test/robots.txt"]


@pytest.mark.parametrize("final", [
    "https://unrelated.test/",
    "https://api.site.test/",
    "https://site.test.evil.test/",
    "https://user@www.site.test/",
    "http://www.site.test/",
    "https://www.site.test:8443/",
])
def test_unsafe_root_redirect_does_not_expand_crawl_scope(monkeypatch, final):
    fetched = []
    async def fetch(url):
        fetched.append(url)
        return response(final, '<a href="/child">Child</a>')
    setup(monkeypatch, fetch, respect_robots=False)
    result = asyncio.run(crawler.crawl("https://site.test/", max_pages=3))
    assert not result.pages
    assert fetched == ["https://site.test/"]
    assert result.skips[0].reason == "not-useful"
    assert result.skips[0].detail == f"redirect-out-of-scope: {final}"


def test_child_redirect_cannot_change_established_canonical_host(monkeypatch):
    fetched = []
    async def fetch(url):
        fetched.append(url)
        if url == "https://site.test":
            return response("https://www.site.test/", '<a href="/child">Child</a>')
        return response("https://unrelated.test/outside", '<a href="https://www.site.test/extra">Extra</a>')
    setup(monkeypatch, fetch, respect_robots=False)
    result = asyncio.run(crawler.crawl("https://site.test", max_pages=3))
    assert [page.url for page in result.pages] == ["https://www.site.test/"]
    assert fetched == ["https://site.test", "https://www.site.test/child"]
    assert result.skips[0].reason == "not-useful"


@pytest.mark.parametrize("start,final,allowed", [
    ("http://site.test:80/", "https://www.site.test:443/", True),
    ("https://site.test:8443/", "https://www.site.test:8443/", True),
    ("https://site.test:8443/", "https://www.site.test:9443/", False),
    ("https://site.test/", "https://www.site.test:bad/", False),
    ("https://site.test/", "file:///site.test/", False),
])
def test_root_canonicalization_preserves_scheme_and_port_constraints(start, final, allowed):
    assert crawler._safe_root_redirect(start, final) is allowed
