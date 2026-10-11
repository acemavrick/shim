import asyncio

from shim import check as C
from shim.manifest import Action, Extract, FromInput, HttpBackend, InputSchema, PropertySchema
from shim.runtime.http import RawResponse


def test_write_passes_on_2xx_without_records(monkeypatch):
    async def fake_send(backend, args):
        return RawResponse(body="Product added.", content_type="text/html", status=200,
                           url=backend.url, bytes=14)
    monkeypatch.setattr(C, "send_http", fake_send)
    a = Action(name="add_to_cart", description="d", tier="reversible",
               input=InputSchema(properties={"prod_id": PropertySchema(type="string", examples=["8", "3"])},
                                 required=["prod_id"]),
               backend=HttpBackend(method="POST", url="http://localhost/api/addtocart", encoding="json",
                                   fields={"prod_id": FromInput(name="prod_id")}),
               extract=Extract(kind="css", fields={}))
    checked, skip = asyncio.run(C.check_action(a, ["x"], allow_writes=True))
    assert skip is None and checked.check.passed


def test_read_still_requires_records(monkeypatch):
    async def fake_send(backend, args):
        return RawResponse(body="<html><body></body></html>", content_type="text/html", status=200,
                           url=backend.url, bytes=20)
    monkeypatch.setattr(C, "send_http", fake_send)
    a = Action(name="search", description="d", tier="read",
               input=InputSchema(properties={"q": PropertySchema(type="string")}, required=[]),
               backend=HttpBackend(method="GET", url="http://localhost/s", fields={"q": FromInput(name="q")}),
               extract=Extract(kind="css", fields={}))
    checked, skip = asyncio.run(C.check_action(a, ["x", "y"]))
    assert skip is not None and not checked.check.passed
