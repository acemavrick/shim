import asyncio
import json

import pytest

from shim import check as checker
from shim.explore.agent import AgentExplorer, AgentResult
from shim.manifest import (Action, Extract, FromGenerate, FromInput, FromPage, HttpBackend,
                           InputSchema, PropertySchema, Session, Source)
from shim.runtime.http import RawResponse
from shim.translate.describe import Proposal


def candidate(extract=None, source="agent-observed", examples=None, tier="read"):
    return Action(
        name="search_books", description="Find books", tier=tier,
        input=InputSchema(properties={"q": PropertySchema(type="string", examples=examples)},
                          required=["q"]),
        backend=HttpBackend(method="GET", url="https://books.test/search",
                            fields={"q": FromInput(name="q")}),
        extract=extract or Extract(kind="json", items="books", fields={"title": "title"}),
        source=Source(page="https://books.test/", form_index=-1, submit=source),
    )


def response(body, status=200):
    return RawResponse(body=body, status=status, content_type="application/json",
                       url="https://books.test/search", bytes=len(body))


def test_verified_agent_proposal_saves_call_but_keeps_two_http_probes(monkeypatch):
    inputs = []

    async def send(backend, args):
        inputs.append(args)
        return response(json.dumps({"books": [{"title": args["q"]}]}))

    async def describe(*args):
        raise AssertionError("already verified model proposal needs no second model call")

    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(candidate(), ["alpha", "beta"], describer=describe))
    assert skip is None and checked.check.passed and checked.check.items_found == 1
    assert checked.name == "search_books" and checked.description == "Find books"
    assert inputs == [{"q": "alpha"}, {"q": "beta"}]


@pytest.mark.parametrize("extract,control_empty", [
    (Extract(kind="json", items="sidebar", fields={"title": "title"}), False),
    (Extract(kind="json", items="missing", fields={"title": "title"}), False),
    (Extract(kind="json", items="books", fields={"title": "title"}), True),
    (Extract(items="[", fields={"title": "h2"}), False),
])
def test_invalid_agent_proposal_falls_back_to_describer(monkeypatch, extract, control_empty):
    calls = []

    async def send(backend, args):
        books = [] if control_empty and args["q"] == "beta" else [{"title": args["q"]}]
        return response(json.dumps({"books": books, "sidebar": [{"title": "Home"}]}))

    async def describe(*args):
        calls.append(args)
        return Proposal(name="find_books", extract=Extract(kind="json", items="books", fields={"title": "title"}))

    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(candidate(extract), ["alpha", "beta"], describer=describe))
    assert calls and checked.name == "find_books" and skip is None


@pytest.mark.parametrize("source,examples", [("form", None), ("agent-observed", ["alpha"])])
def test_fast_path_requires_agent_provenance_and_distinct_control(monkeypatch, source, examples):
    calls = []

    async def send(backend, args):
        return response(json.dumps({"books": [{"title": args["q"]}]}))

    async def describe(*args):
        calls.append(args)
        return Proposal()

    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(candidate(source=source, examples=examples), ["alpha", "beta"], describer=describe))
    if examples == ["alpha"]:
        assert len(calls) == 0 and skip is not None and not checked.check.passed
        assert "distinct control input" in checked.check.error
    else:
        assert len(calls) == 1


def test_failed_control_cannot_ship_or_reuse_proposal(monkeypatch):
    async def send(backend, args):
        return response('{"books":[{"title":"Alpha"}]}', status=200 if args["q"] == "alpha" else 403)

    async def describe(*args):
        raise AssertionError("failed control is rejected before translation")

    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(candidate(), ["alpha", "beta"], describer=describe))
    assert skip is not None and not checked.check.passed


def test_reuse_prunes_empty_css_fields_before_verification(monkeypatch):
    async def send(backend, args):
        return response(f'<div class="book"><h2>{args["q"]}</h2></div>')

    async def describe(*args):
        raise AssertionError("nonempty CSS extraction already verified")

    monkeypatch.setattr(checker, "send_http", send)
    action = candidate(Extract(items=".book", fields={"title": "h2", "missing": ".absent"}))
    checked, skip = asyncio.run(checker.check_action(action, ["alpha", "beta"], describer=describe))
    assert skip is None and checked.extract.fields == {"title": "h2"}


@pytest.mark.parametrize("valid", [True, False])
def test_static_agent_get_reuses_only_nonempty_extract(monkeypatch, valid):
    inputs, calls = [], []

    async def send(backend, args):
        inputs.append(args)
        return response('{"books":[{"title":"Site introduction"}]}')

    async def describe(*args):
        calls.append(args)
        return Proposal(extract=Extract(kind="json", items="books", fields={"title": "title"}))

    monkeypatch.setattr(checker, "send_http", send)
    action = candidate(Extract(kind="json", items="books" if valid else "missing", fields={"title": "title"}))
    action.input = InputSchema()
    action.backend.fields = {}
    checked, skip = asyncio.run(checker.check_action(action, ["alpha", "beta"], describer=describe))
    assert skip is None and checked.check.passed
    assert inputs == [{}] and len(calls) == (0 if valid else 1)


@pytest.mark.parametrize("examples", [None, ["pinned"], ["pinned", "control"]])
def test_repeated_primary_arguments_do_not_repeat_failed_model_proposal(monkeypatch, examples):
    inputs, calls = [], []

    async def send(backend, args):
        inputs.append(args)
        return response('{"books":[]}')

    async def describe(*args):
        calls.append(args)
        return Proposal(extract=Extract(kind="json", items="books", fields={"title": "title"}))

    monkeypatch.setattr(checker, "send_http", send)
    action = candidate(examples=examples)
    if examples is None:
        action.input = InputSchema()
        action.backend.fields = {}
    checked, skip = asyncio.run(checker.check_action(action, ["alpha", "beta", "gamma", "delta"], describer=describe))
    assert skip is not None and not checked.check.passed
    if examples == ["pinned"]:
        assert "distinct control input" in checked.check.error
        assert not calls and inputs == [{"q": "pinned"}]
        return
    assert checked.check.error == "no records extracted from the response"
    assert len(calls) == 1
    assert inputs == ([{}] if examples is None else [{"q": value} for value in examples])


def test_distinct_primary_search_inputs_still_retry_failed_extraction(monkeypatch):
    inputs, calls = [], []

    async def send(backend, args):
        inputs.append(args)
        books = [] if args["q"] == "alpha" else [{"title": args["q"]}]
        return response(json.dumps({"books": books}))

    async def describe(*args):
        calls.append(args)
        return Proposal(extract=Extract(kind="json", items="books", fields={"title": "title"}))

    monkeypatch.setattr(checker, "send_http", send)
    checked, skip = asyncio.run(checker.check_action(candidate(source="form"), ["alpha", "beta"], describer=describe))
    assert skip is None and checked.check.passed and checked.check.input == {"q": "beta"}
    assert len(calls) == 2
    assert inputs == [{"q": "alpha"}, {"q": "beta"}, {"q": "beta"}, {"q": "alpha"}]


def test_independent_read_probes_overlap_and_still_verify_distinct_records(monkeypatch):
    async def run():
        started = []
        both_started = asyncio.Event()

        async def send(backend, args):
            started.append(args)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            return response(json.dumps({"books": [{"title": args["q"]}]}))

        monkeypatch.setattr(checker, "send_http", send)
        checked, skip = await asyncio.wait_for(checker.check_action(candidate(), ["alpha", "beta"]), 0.5)
        assert skip is None and checked.check.passed
        assert started == [{"q": "alpha"}, {"q": "beta"}]

    asyncio.run(run())


@pytest.mark.parametrize("dependency", ["post", "write", "cookies", "session", "refetch", "page", "generate"])
def test_dependent_probes_remain_sequential(monkeypatch, dependency):
    active = peak = 0
    inputs = []

    async def send(backend, args, **kwargs):
        nonlocal active, peak
        if dependency == "post":
            assert kwargs.get("require_read_policy") is True
        inputs.append(args)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return response(json.dumps({"books": [{"title": args["q"]}]}))
        finally:
            active -= 1

    monkeypatch.setattr(checker, "send_http", send)
    action = candidate()
    if dependency == "post":
        action.backend.method = "POST"
    elif dependency == "write":
        action.tier = "reversible"
        action.backend.method = "POST"
    elif dependency == "cookies":
        action.session = Session(carry_cookies=True)
    elif dependency == "session":
        action.session = Session(scope="per-connection")
    elif dependency == "refetch":
        action.backend.refetch_from = "https://books.test/"
    elif dependency == "page":
        action.backend.fields["csrf"] = FromPage(field="csrf")
    elif dependency == "generate":
        action.backend.fields["request_id"] = FromGenerate()
    checked, skip = asyncio.run(checker.check_action(action, ["alpha", "beta"], allow_writes=True))
    assert skip is None and checked.check.passed and peak == 1
    assert inputs == ([{"q": "alpha"}] if dependency == "write" else [{"q": "alpha"}, {"q": "beta"}])


def test_cancelling_parallel_probes_drains_both_requests(monkeypatch):
    async def run():
        active = 0
        both_started = asyncio.Event()

        async def send(backend, args):
            nonlocal active
            active += 1
            if active == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        monkeypatch.setattr(checker, "send_http", send)
        task = asyncio.create_task(checker.check_action(candidate(), ["alpha", "beta"]))
        await asyncio.wait_for(both_started.wait(), 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0

    asyncio.run(run())


@pytest.mark.parametrize("blocked", [False, True])
def test_failed_primary_cancels_control_and_preserves_failure(monkeypatch, blocked):
    async def run():
        control_started = asyncio.Event()
        control_drained = False

        async def send(backend, args):
            nonlocal control_drained
            if args["q"] == "alpha":
                await control_started.wait()
                if blocked:
                    return response("blocked", 403)
                raise RuntimeError("primary unavailable")
            control_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                control_drained = True

        async def describe(*args):
            raise AssertionError("failed primary cannot call the model")

        monkeypatch.setattr(checker, "send_http", send)
        checked, skip = await asyncio.wait_for(checker.check_action(candidate(examples=["alpha", "beta"]),
                                             ["one", "two"], describer=describe), 0.5)
        assert control_drained and skip is not None and not checked.check.passed
        assert checked.check.error == ("bot protection" if blocked else "RuntimeError: primary unavailable")

    asyncio.run(run())


@pytest.mark.parametrize("retry", [False, True])
def test_parallel_control_failure_keeps_error_attribution_and_next_input_retry(monkeypatch, retry):
    seen = []

    async def send(backend, args):
        seen.append(args["q"])
        if seen == ["alpha", "beta"]:
            raise RuntimeError("control unavailable")
        return response(json.dumps({"books": [{"title": args["q"]}]}))

    monkeypatch.setattr(checker, "send_http", send)
    action = candidate(examples=None if retry else ["alpha", "beta"])
    checked, skip = asyncio.run(checker.check_action(action, ["alpha", "beta"]))
    if retry:
        assert skip is None and checked.check.passed and checked.check.input == {"q": "beta"}
        assert seen == ["alpha", "beta", "beta", "alpha"]
    else:
        assert skip is not None and not checked.check.passed
        assert checked.check.error == "control probe failed: RuntimeError: control unavailable"
        assert seen == ["alpha", "beta"]


def test_write_down_retries_invalid_json_and_counts_actual_calls():
    prompts = []

    async def complete(system, user):
        prompts.append(user)
        return "no json" if len(prompts) == 1 else '{"summary":"observed no tools"}'

    explorer = AgentExplorer(complete, "Read books")
    result = AgentResult()
    asyncio.run(explorer._write_down(result))
    assert explorer.model_calls == 2 and result.summary == "observed no tools" and not result.errors
    assert "previous reply was invalid" in prompts[1]


def test_write_down_does_not_repeat_unrepairable_provider_failure():
    async def complete(system, user):
        raise RuntimeError("provider unavailable")

    explorer = AgentExplorer(complete, "Read books")
    result = AgentResult()
    asyncio.run(explorer._write_down(result))
    assert explorer.model_calls == 1 and result.errors == ["RuntimeError: provider unavailable"]


@pytest.mark.parametrize("method", ["_write_down", "_ask_step"])
def test_cancellation_propagates_without_model_retry(method):
    async def complete(system, user):
        raise asyncio.CancelledError()

    explorer = AgentExplorer(complete, "Read books")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(explorer, method)(AgentResult() if method == "_write_down" else "prompt"))
    assert explorer.model_calls == 1
