import asyncio
from types import SimpleNamespace

from shim.explore.agent import AgentExplorer


def test_exploration_blocks_post_until_writes_are_enabled():
    async def run(allow_writes):
        outcomes = []
        async def abort():
            outcomes.append("blocked")
        async def continue_():
            outcomes.append("sent")
        route = SimpleNamespace(request=SimpleNamespace(method="POST", url="https://shop.test/cart"),
                                abort=abort, continue_=continue_)
        explorer = AgentExplorer(None, "Find a laptop", allow_writes=allow_writes)
        await explorer._route_request(route)
        return outcomes
    assert asyncio.run(run(False)) == ["blocked"]
    assert asyncio.run(run(True)) == ["sent"]


def test_exploration_allows_get_without_write_permission():
    async def run():
        outcomes = []
        async def abort():
            outcomes.append("blocked")
        async def continue_():
            outcomes.append("sent")
        route = SimpleNamespace(request=SimpleNamespace(method="GET", url="https://shop.test/products"),
                                abort=abort, continue_=continue_)
        await AgentExplorer(None, "Find a laptop")._route_request(route)
        return outcomes
    assert asyncio.run(run()) == ["sent"]


def test_blocked_request_evidence_reaches_model_without_repeated_warnings():
    async def run():
        events, prompts = [], []
        async def complete(system, prompt):
            prompts.append(prompt)
            return '{"action":"done"}'
        async def abort():
            pass
        explorer = AgentExplorer(complete, "Read products", on_event=events.append)
        route = SimpleNamespace(request=SimpleNamespace(method="POST", url="https://shop.test/variant", resource_type="fetch"), abort=abort)
        await explorer._route_request(route)
        await explorer._route_request(route)
        await explorer._ask_step("Page")
        assert len(events) == 1
        assert events[0]["method"] == "POST"
        assert "https://shop.test/variant" in prompts[0]
        assert "safety is not established" in prompts[0]
    asyncio.run(run())
