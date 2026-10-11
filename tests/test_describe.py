import asyncio
import json

from shim.check import check_action
from shim.manifest import Action, Extract, FromInput, HttpBackend, InputSchema, PropertySchema, Source
from shim.translate.describe import ModelDescriber, apply_proposal, parse_proposal


def _action(tier="read"):
    return Action(
        name="submit_x", description="heuristic", tier=tier,
        input=InputSchema(properties={"q": PropertySchema(type="string")}, required=[]),
        backend=HttpBackend(method="GET", url="https://x.test/s", fields={"q": FromInput(name="q")}),
        extract=Extract(kind="css", fields={}),
        source=Source(page="https://x.test/", form_index=0),
    )


def test_parse_tolerates_prose_around_json():
    p = parse_proposal('Sure! {"useful": true, "name": "search_books", "tier": "read"} done')
    assert p.name == "search_books"


def test_tier_only_escalates():
    a = _action(tier="irreversible")
    apply_proposal(a, parse_proposal('{"name": "search_books", "tier": "read"}'))
    assert a.tier == "irreversible", "a model may not declare a dangerous action safe"
    b = _action(tier="read")
    apply_proposal(b, parse_proposal('{"tier": "irreversible"}'))
    assert b.tier == "irreversible"


def test_invalid_name_is_rejected_then_retried_then_gives_up():
    replies = iter(['{"name": "Bad Name!"}', 'still not json'])

    async def fake(system, user):
        return next(replies)

    d = ModelDescriber(fake)
    out = asyncio.run(d(_action(), "<html></html>", None, {}, {}))
    assert out is None and d.failures == 1 and d.calls == 1


def test_network_error_never_breaks_compile():
    async def boom(system, user):
        raise RuntimeError("api down")

    d = ModelDescriber(boom)
    assert asyncio.run(d(_action(), "<html></html>", None, {}, {})) is None
