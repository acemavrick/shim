import asyncio
import json

import httpx
import pytest

from shim import compile as compiler, web
from shim.config import Config
from shim.manifest import Action, CheckResult, Extract, HttpBackend, InputSchema, Manifest, PropertySchema


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "SITES_DIR", tmp_path)
    monkeypatch.setattr(web, "SITES_DIR", tmp_path)
    action = Action(name="search", description="Search books", tier="read",
                    input=InputSchema(properties={"query": PropertySchema(type="string"),
                                                  "limit": PropertySchema(type="integer", minimum=1)}, required=["query"]),
                    backend=HttpBackend(method="GET", url="https://books.test/search"),
                    extract=Extract(fields={"title": "h1"}), check=CheckResult(passed=True))
    manifest = Manifest(site="https://books.test", host="books.test", compiled_at="2026-10-10T12:00:00Z",
                        intent="Find a book", actions=[action])
    compiler.save_manifest(manifest)
    return manifest


def test_library_preserves_site_intent(saved):
    data = request("GET", "/api/sites").json()["sites"][0]
    assert data["intent"] == "Find a book"
    assert data["actions"] == 1


def test_missing_saved_site_is_a_useful_404(saved):
    response = request("GET", "/api/manifest/missing.test")
    assert response.status_code == 404


@pytest.mark.parametrize("arguments", [{}, {"query": "books", "limit": 0}, {"query": 2}, {"query": "books", "unknown": 1}])
def test_invalid_tool_inputs_are_rejected_before_network(saved, arguments, monkeypatch):
    async def forbidden(*args):
        pytest.fail("invalid inputs must never reach the website")
    monkeypatch.setattr(web, "invoke_action", forbidden)
    response = request("POST", "/api/call", json={"host": "books.test", "action": "search", "arguments": arguments})
    assert response.status_code == 422


def test_compile_automatically_explores_with_intent_and_key(monkeypatch):
    monkeypatch.setattr(web, "config", lambda: Config(anthropic_api_key="test"), raising=False)
    async def compile_site(url, **kwargs):
        await kwargs["emit"]({"type": "done", "agent": kwargs["agent"], "writes": kwargs["allow_writes"]})
    monkeypatch.setattr(web, "compile_site", compile_site)
    response = request("GET", "/api/compile", params={"url": "https://books.test", "intent": "Find a book"})
    event = json.loads(response.text.removeprefix("data: ").strip())
    assert event == {"type": "done", "agent": True, "writes": False}


def test_compile_rejects_non_web_urls():
    assert request("GET", "/api/compile", params={"url": "file:///etc/passwd"}).status_code == 422


@pytest.mark.parametrize("agent", [None, "true"])
def test_empty_intent_still_explores_with_key(monkeypatch, agent):
    monkeypatch.setattr(web, "config", lambda: Config(anthropic_api_key="test"))
    async def compile_site(url, **kwargs):
        await kwargs["emit"]({"type": "probe", "explores": kwargs["agent"]})
    monkeypatch.setattr(web, "compile_site", compile_site)
    params = {"url": "https://books.test"}
    if agent is not None:
        params["agent"] = agent
    response = request("GET", "/api/compile", params=params)
    assert response.status_code == 200
    assert json.loads(response.text.removeprefix("data: ").strip())["explores"] is True


def test_explicit_exploration_without_key_is_reported(monkeypatch):
    monkeypatch.setattr(web, "config", lambda: Config(), raising=False)
    response = request("GET", "/api/compile", params={"url": "https://books.test", "intent": "Find a book", "agent": "true"})
    assert response.status_code == 422


def test_irreversible_call_requires_confirmation(saved, monkeypatch):
    saved.actions[0].tier = "irreversible"
    compiler.save_manifest(saved)
    async def forbidden(*args):
        pytest.fail("unconfirmed action must not execute")
    monkeypatch.setattr(web, "invoke_action", forbidden)
    response = request("POST", "/api/call", json={"host": "books.test", "action": "search", "arguments": {"query": "books"}})
    assert response.json()["status"] == "confirmation_required"
