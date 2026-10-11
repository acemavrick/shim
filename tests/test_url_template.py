from shim.manifest import HttpBackend
from shim.runtime import http as rt


def test_path_placeholder_is_filled_and_encoded(monkeypatch):
    seen = {}

    async def fake_fetch(method, url, **kw):
        seen.update(method=method, url=url, params=kw.get("params"))
        return rt.RawResponse(body="", content_type="text/html", status=200, url=url, bytes=0)

    monkeypatch.setattr(rt, "_polite_fetch", fake_fetch)
    import asyncio
    backend = HttpBackend(method="GET", url="https://x.test/ebooks/{id}", fields={})
    asyncio.run(rt.send_http(backend, {"id": "13 42/x"}))
    assert seen["url"] == "https://x.test/ebooks/13%2042%2Fx"
