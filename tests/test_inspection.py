import asyncio
import json

from shim.explore.agent import AgentExplorer, Observed


def test_inspection_actions_are_accepted_but_arbitrary_code_is_not():
    async def complete(system, prompt):
        return '{"action":"inspect","tool":"network"}'
    act = asyncio.run(AgentExplorer(complete, "Learn site")._ask_step("page"))
    assert act is not None and act.tool == "network"


def test_network_inspection_is_bounded_and_does_not_expose_post_bodies():
    from shim.explore.inspect import inspect_page
    observed = [Observed(step=1, method="GET", url=f"https://site.test/{i}",
                         resource_type="fetch", status=200, content_type="application/json",
                         sample="x" * 9000, post_data="password=secret") for i in range(100)]
    result = asyncio.run(inspect_page(None, "network", observed=observed))
    assert len(result["requests"]) == 12
    assert len(json.dumps(result)) < 9000
    assert "password" not in json.dumps(result)


def test_unknown_inspection_cannot_execute_code():
    from shim.explore.inspect import inspect_page
    result = asyncio.run(inspect_page(None, "eval", selector="process.exit()"))
    assert result["error"]
