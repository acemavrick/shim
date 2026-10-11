import asyncio
from types import SimpleNamespace

from shim import compile as compiler
from shim.config import Config
from shim.explore.agent import AgentResult
from shim.explore.crawl import CrawledPage
from shim.manifest import Action, CheckResult, Extract, FromInput, HttpBackend, InputSchema, PropertySchema, Step


def test_intent_compiles_saved_site_and_keeps_procedure_names_current(tmp_path, monkeypatch):
    from shim import config
    from shim.explore import agent
    from shim.translate import describe
    monkeypatch.setattr(compiler, "SITES_DIR", tmp_path)
    monkeypatch.setattr(config, "config", lambda: Config(anthropic_api_key="test"))
    monkeypatch.setattr(describe, "AnthropicCompleter", lambda cfg, **kwargs: None)
    async def crawl(*args, **kwargs):
        return SimpleNamespace(pages=[CrawledPage(url="https://shop.test", html="<main>Loading...</main>")],
                               forms=[], skips=[], pages_failed=0, blocked=False)
    monkeypatch.setattr(compiler, "crawl", crawl)
    action = Action(name="get_product", description="Read product", tier="read",
                    input=InputSchema(properties={"id": PropertySchema(type="string", examples=["8", "9"])}, required=["id"]),
                    backend=HttpBackend(method="GET", url="https://shop.test/product", fields={"id": FromInput(name="id")}),
                    extract=Extract(kind="json", fields={"name": "name"}))
    class Explorer:
        def __init__(self, complete, intent, **kwargs):
            pass
        async def run(self, url):
            return AgentResult(tools=[action], procedure=[Step.model_validate({"action": "get_product", "with": {"id": "{id}"}})],
                               pages=[CrawledPage(url=url, html='<html><h1>Shop</h1></html>')])
    monkeypatch.setattr(agent, "AgentExplorer", Explorer)
    async def check(action, *args, **kwargs):
        action.name = "get_product_details"
        action.check = CheckResult(passed=True, input={"id": "8"}, status=200, items_found=1)
        return action, None
    monkeypatch.setattr(compiler, "check_action", check)
    manifest = asyncio.run(compiler.compile_site("https://shop.test", intent="Find a product"))
    assert [a.name for a in manifest.actions] == ["get_product_details"]
    assert manifest.procedures[0].steps[0].action == "get_product_details"
    assert compiler.load_manifest("shop.test").procedures[0].steps[0].action == "get_product_details"
    assert 'get_product_details(id={id})' in (tmp_path / "shop.test/agent/llms.txt").read_text()
    assert (tmp_path / "shop.test/agent/index.html").is_file()
    assert manifest.pages[0].markdown == '# Shop'


def test_explicit_agent_without_key_fails_before_crawling(monkeypatch):
    import pytest
    from shim import config
    monkeypatch.setattr(config, "config", lambda: Config())
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration must be rejected before crawling")
    monkeypatch.setattr(compiler, "crawl", forbidden)
    with pytest.raises(ValueError, match="key"):
        asyncio.run(compiler.compile_site("https://shop.test", agent=True, intent="Find a product"))
