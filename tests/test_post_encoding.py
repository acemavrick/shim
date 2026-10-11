import asyncio

from shim.manifest import FromConst, FromInput, HttpBackend
from shim.runtime import http as rt


def _capture(monkeypatch):
    seen = {}

    async def fake(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return rt.RawResponse(body="", content_type="text/html", status=200, url=url, bytes=0)

    monkeypatch.setattr(rt, "_polite_fetch", fake)
    return seen


def test_form_post_is_urlencoded_with_repeats(monkeypatch):
    seen = _capture(monkeypatch)
    backend = HttpBackend(method="POST", url="https://x.test/cart", encoding="form",
                          fields={"q": FromInput(name="q"), "tag": FromConst(value="a b")})
    asyncio.run(rt.send_http(backend, {"q": "x&y"}))
    assert seen["method"] == "POST"
    assert seen["content"] == "q=x%26y&tag=a+b"
    assert seen["headers"]["content-type"] == "application/x-www-form-urlencoded"


def test_json_post(monkeypatch):
    seen = _capture(monkeypatch)
    backend = HttpBackend(method="POST", url="https://x.test/api", encoding="json",
                          fields={"q": FromInput(name="q")})
    asyncio.run(rt.send_http(backend, {"q": "laptop"}))
    assert seen["content"] == '{"q": "laptop"}'
    assert seen["headers"]["content-type"] == "application/json"
