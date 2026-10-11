import asyncio

from shim.explore.agent import _ToolSpec, _tool_to_action
from shim.manifest import HttpBackend, FromConst, FromGenerate
from shim.runtime import http as rt


def _spec(**kw):
    base = dict(name="x", description="d", tier="read", method="POST", url="https://x.test/api/view",
                encoding="json", inputs={}, constants={})
    base.update(kw)
    return _ToolSpec(**base)


def test_post_read_accepted_only_with_read_hint():
    assert _tool_to_action(_spec(name="get_product_details", inputs={"id": {"wire": "id", "in": "body", "examples": ["8", "9"]}}), "").tier == "read"
    assert _tool_to_action(_spec(name="add_to_cart", url="https://x.test/api/addtocart",
                                 inputs={"id": {"wire": "id", "in": "body", "examples": ["8", "9"]}}), "").tier == "reversible"


def test_uuid_inputs_are_generated_and_tokens_pinned():
    a = _tool_to_action(_spec(name="add_to_cart", tier="reversible", inputs={
        "id": {"wire": "id", "in": "body", "examples": ["dd98e6ce-8c7e-6e56-c207-49e05ecb283c", "0f0f0f0f-1111-2222-3333-444444444444"]},
        "cookie": {"wire": "cookie", "in": "body", "examples": ["user=34b5590f-6f16-2478-2299-40e0e332656e"]},
        "prod_id": {"wire": "prod_id", "in": "body", "examples": ["8", "3"]},
    }, constants={"flag": False}), "")
    assert list(a.input.properties) == ["prod_id"], "only prod_id is the agent's to choose"
    assert isinstance(a.backend.fields["id"], FromGenerate)
    assert isinstance(a.backend.fields["cookie"], FromConst)
    assert a.backend.fields["flag"].value is False, "typed constant survives"
    assert "session token" in a.description


def test_generate_and_typed_json_body(monkeypatch):
    seen = {}

    async def fake(method, url, **kw):
        seen.update(kw)
        return rt.RawResponse(body="", content_type="application/json", status=200, url=url, bytes=0)

    monkeypatch.setattr(rt, "_polite_fetch", fake)
    backend = HttpBackend(method="POST", url="https://x.test/api/addtocart", encoding="json",
                          fields={"id": FromGenerate(kind="uuid"), "prod_id": FromConst(value=8),
                                  "flag": FromConst(value=False)})
    asyncio.run(rt.send_http(backend, {}))
    import json
    body = json.loads(seen["content"])
    assert body["prod_id"] == 8 and body["flag"] is False and len(body["id"]) == 36
    assert seen["headers"]["origin"] == "https://x.test"
