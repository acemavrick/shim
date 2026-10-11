import asyncio
from types import SimpleNamespace

import pytest

from shim.config import Config
from shim.translate.describe import AnthropicCompleter


@pytest.mark.parametrize("fails", [False, True])
def test_model_events_report_actual_usage_or_failure(monkeypatch, fails):
    import anthropic
    async def create(**kwargs):
        if fails:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text="result")],
                               usage=SimpleNamespace(input_tokens=321, output_tokens=42))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kwargs:
                        SimpleNamespace(messages=SimpleNamespace(create=create)))
    events = []
    complete = AnthropicCompleter(Config(anthropic_api_key="secret", model="test-model"), on_event=events.append)
    if fails:
        with pytest.raises(RuntimeError):
            asyncio.run(complete("system", "user"))
        assert events[-1]["type"] == "model_error"
    else:
        assert asyncio.run(complete("system", "user")) == "result"
        assert events[-1]["input_tokens"] == 321
        assert events[-1]["output_tokens"] == 42
    assert events[0]["type"] == "model_start"
    assert events[-1]["elapsed_ms"] >= 0
    assert "secret" not in str(events)
