import asyncio

from shim import check as checker
from shim.manifest import Action, Extract, FromInput, HttpBackend, InputSchema, PropertySchema
from shim.runtime.http import RawResponse
from shim.translate.describe import Proposal


def action():
    return Action(name="search", description="Search", tier="read",
                  input=InputSchema(properties={"q": PropertySchema(type="string", examples=["laptop", "phone"])}, required=["q"]),
                  backend=HttpBackend(method="GET", url="https://shop.test/search", fields={"q": FromInput(name="q")}),
                  extract=Extract(kind="json", items="products", fields={"name": "name"}))


def test_json_read_must_change_with_input(monkeypatch):
    async def send(backend, args):
        return RawResponse(body='{"products":[{"name":"constant sidebar"}]}', content_type="application/json", status=200,
                           url=backend.url, bytes=48)
    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(action(), ["laptop", "phone"]))
    assert skip is not None and not checked.check.passed


def test_valid_model_extract_does_not_run_heuristic_inference(monkeypatch):
    async def send(backend, args):
        return RawResponse(body=f'<html><div class="result"><h2>{args["q"]}</h2></div></html>',
                           content_type="text/html", status=200, url=backend.url, bytes=70)
    async def describe(*args):
        return Proposal(name="search_products", extract=Extract(items=".result", fields={"name": "h2"}))
    def forbidden(*args):
        raise AssertionError("valid model proposal must not depend on heuristic inference")
    monkeypatch.setattr(checker, "send_http", send)
    monkeypatch.setattr(checker, "infer_by_diff", forbidden)
    monkeypatch.setattr(checker, "infer_listing", forbidden)
    checked, skip = asyncio.run(checker.check_action(action(), ["laptop", "phone"], describer=describe))
    assert skip is None and checked.name == "search_products" and checked.check.passed


def test_failed_model_proposal_does_not_ship_heuristic_tool(monkeypatch):
    async def send(backend, args):
        return RawResponse(body=f'{{"products":[{{"name":"{args["q"]}"}}]}}', content_type="application/json", status=200,
                           url=backend.url, bytes=48)
    async def describe(*args):
        return None
    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(action(), ["laptop", "phone"], describer=describe))
    assert skip is not None and not checked.check.passed
