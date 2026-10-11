"""POST reads: observed evidence is the authorization, not an operator rule."""
import asyncio
import json
from types import SimpleNamespace

from shim import check as C
from shim.explore.agent import AgentExplorer
from shim.manifest import Action, Extract, FromInput, HttpBackend, InputSchema, PropertySchema, Source
from shim.runtime.http import RawResponse
from shim.safety import exploration_request_allowed


# ------------------------------------------------------------ exploration

def test_exploration_allows_lookup_post_without_write_permission():
    assert exploration_request_allowed("POST", "https://api.shop.test/bycat", {"cat": "notebook"},
                                       allow_writes=False, rules=[])


def test_exploration_blocks_only_money_deletion_and_credentials():
    blocked = [
        ("https://shop.test/checkout", {"id": "8"}),
        ("https://shop.test/api/orders", {"sku": "8"}),
        ("https://shop.test/api/items/delete", {"id": "8"}),
        ("https://shop.test/api/search", {"q": "x", "password": "hunter2"}),
        ("https://shop.test/graphql", {"query": "mutation { placeOrder(id: 1) { ok } }"}),
    ]
    for url, body in blocked:
        assert not exploration_request_allowed("POST", url, body, allow_writes=False, rules=[]), url
    # Reversible and harmless: a visitor does these without thinking.
    allowed = [
        ("https://shop.test/cart", {"id": "8"}),
        ("https://shop.test/api/subscribe", {"email": "a@b.test"}),
        ("https://shop.test/graphql", {"query": "mutation { addToCart(id: 1) { ok } }"}),
        ("https://shop.test/login", {"user": "x"}),
    ]
    for url, body in allowed:
        assert exploration_request_allowed("POST", url, body, allow_writes=False, rules=[]), url


def test_exploration_blocks_put_patch_delete_without_write_permission():
    for method in ("PUT", "PATCH", "DELETE"):
        assert not exploration_request_allowed(method, "https://shop.test/api/items/1", {},
                                               allow_writes=False, rules=[]), method


def test_exploration_allows_everything_with_write_permission():
    assert exploration_request_allowed("POST", "https://shop.test/checkout", {"id": "8"},
                                       allow_writes=True, rules=[])
    assert exploration_request_allowed("DELETE", "https://shop.test/api/items/1", {},
                                       allow_writes=True, rules=[])


def test_route_request_lets_lookup_post_through():
    async def run():
        outcomes = []
        async def abort():
            outcomes.append("blocked")
        async def continue_():
            outcomes.append("sent")
        request = SimpleNamespace(method="POST", url="https://api.shop.test/bycat",
                                  post_data_json={"cat": "notebook"}, resource_type="fetch")
        route = SimpleNamespace(request=request, abort=abort, continue_=continue_)
        await AgentExplorer(None, "Find a laptop", allow_writes=False)._route_request(route)
        return outcomes
    assert asyncio.run(run()) == ["sent"]


# ------------------------------------------------------------------ check

def _read_post(source: Source | None) -> Action:
    return Action(
        name="list_products_by_category", description="d", tier="read",
        input=InputSchema(properties={"cat": PropertySchema(type="string", examples=["notebook", "phone"])},
                          required=["cat"]),
        backend=HttpBackend(method="POST", url="https://api.shop.test/bycat", encoding="json",
                            fields={"cat": FromInput(name="cat")}),
        extract=Extract(kind="json", items="Items", fields={"title": "title"}),
        source=source,
    )


def _varying_send():
    async def fake_send(backend, args, **kwargs):
        body = json.dumps({"Items": [{"title": f"{args['cat']} one"}, {"title": f"{args['cat']} two"}]})
        return RawResponse(body=body, content_type="application/json", status=200, url=backend.url, bytes=len(body))
    return fake_send


def test_check_ships_observed_read_post_whose_records_vary(monkeypatch):
    monkeypatch.setattr(C, "send_http", _varying_send())
    action = _read_post(Source(page="https://shop.test", form_index=-1, submit="agent-observed"))
    checked, skip = asyncio.run(C.check_action(action, ["x", "y"]))
    assert skip is None and checked.check.passed and checked.check.items_found == 2


def test_check_rejects_read_post_without_observed_evidence(monkeypatch):
    monkeypatch.setattr(C, "send_http", _varying_send())
    checked, skip = asyncio.run(C.check_action(_read_post(None), ["x", "y"]))
    assert skip is not None and not checked.check.passed
    assert "observed" in (checked.check.error or "")


def test_check_rejects_observed_read_post_whose_records_do_not_vary(monkeypatch):
    async def constant(backend, args, **kwargs):
        body = json.dumps({"Items": [{"title": "Product added."}]})
        return RawResponse(body=body, content_type="application/json", status=200, url=backend.url, bytes=len(body))
    monkeypatch.setattr(C, "send_http", constant)
    action = _read_post(Source(page="https://shop.test", form_index=-1, submit="agent-observed"))
    checked, skip = asyncio.run(C.check_action(action, ["x", "y"]))
    assert skip is not None and not checked.check.passed


# ------------------------------------------------------------------ serve

def test_verified_read_post_is_served_without_operator_rule(monkeypatch):
    import httpx
    from shim.runtime import http
    from shim.config import Config
    import shim.config as cfg
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"Items": [{"title": "Sony vaio i5"}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(http, "_get_client", lambda: client)
    monkeypatch.setattr(cfg, "config", lambda: Config())
    action = _read_post(Source(page="https://shop.test", form_index=-1, submit="agent-observed"))
    async def run():
        try:
            return await http.invoke_action(action, {"cat": "notebook"})
        finally:
            await client.aclose()
    result = asyncio.run(run())
    assert result.status == 200 and result.records == [{"title": "Sony vaio i5"}]
    assert seen == [{"cat": "notebook"}]


# ------------------------------------------------- single-example leniency

def test_detail_tool_with_one_example_ships_on_a_single_verified_probe(monkeypatch):
    calls = []
    async def fake_send(backend, args, **kwargs):
        calls.append(args)
        body = "<html><body><h1>AirPods Pro</h1><p class='lede'>Great.</p></body></html>"
        return RawResponse(body=body, content_type="text/html", status=200, url=backend.url, bytes=len(body))
    monkeypatch.setattr(C, "send_http", fake_send)
    action = Action(
        name="get_product_overview", description="d", tier="read",
        input=InputSchema(properties={"slug": PropertySchema(type="string", examples=["airpods-pro"])},
                          required=["slug"]),
        backend=HttpBackend(method="GET", url="https://x.test/{slug}/"),
        extract=Extract(kind="css", fields={"title": "h1", "lede": ".lede"}),
        source=Source(page="https://x.test/", form_index=-1, submit="agent-observed"),
    )
    checked, skip = asyncio.run(C.check_action(action, ["alpha", "beta"]))
    assert skip is None and checked.check.passed
    assert checked.check.control_input is None
    assert calls == [{"slug": "airpods-pro"}]
