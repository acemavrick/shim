import asyncio
import json
import os
import sys

import pytest

from shim.config import Config, load_config
from shim.translate.codex import CodexCLICompleter
from shim.translate.describe import AnthropicCompleter, make_completer


def fake_cli(tmp_path, body):
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


def test_both_model_factories_and_auto_selection(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kwargs: object())
    assert isinstance(make_completer(Config(anthropic_api_key="test")), AnthropicCompleter)
    assert Config(model_provider="auto", anthropic_api_key="test").provider == "anthropic"
    assert Config(model_provider="auto").provider == "codex-cli"
    assert isinstance(make_completer(Config(model_provider="codex-cli")), CodexCLICompleter)


def test_cli_reads_stdin_handles_events_and_reuses_login(tmp_path):
    args_path = tmp_path / "args.json"
    binary = fake_cli(tmp_path, f'''import json,sys,os
instructions_path=json.loads(next(a.split("=",1)[1] for a in sys.argv if a.startswith("model_instructions_file=")))
json.dump({{"args":sys.argv,"prompt":sys.stdin.read(),"cwd":os.getcwd(),"instructions":open(instructions_path).read()}},open({str(args_path)!r},"w"))
print("diagnostic line")
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":'{{"ok":true}}'}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":12,"cached_input_tokens":4,"output_tokens":8}}}}))
''')
    events = []
    cfg = Config(model_provider="codex-cli", codex_binary=binary, codex_model="test-model")
    assert cfg.has_model
    result = asyncio.run(make_completer(cfg, on_event=events.append)("System instruction", "Page evidence"))
    assert json.loads(result) == {"ok": True}
    called = json.loads(args_path.read_text())
    assert "Page evidence" in called["prompt"]
    assert "--ephemeral" in called["args"] and "--ignore-user-config" in called["args"]
    assert "System instruction" in called["instructions"]
    assert "features.shell_tool=false" in called["args"]
    assert "read-only" in called["args"]
    assert called["cwd"] != str(tmp_path)
    assert events[-1]["cached_input_tokens"] == 4


@pytest.mark.parametrize("mode", ["failure", "timeout", "empty"])
def test_cli_failures_are_visible_and_timeout_is_bounded(tmp_path, mode):
    body = {"failure": 'import sys\nprint(\'{"type":"turn.failed","error":{"message":"login required"}}\')\nsys.exit(1)\n',
            "timeout": "import time\ntime.sleep(30)\n", "empty": "print('not a result')\n"}[mode]
    binary = fake_cli(tmp_path, body)
    events = []
    cfg = Config(model_provider="codex-cli", codex_binary=binary, model_timeout=0.2 if mode == "timeout" else 5)
    with pytest.raises(TimeoutError if mode == "timeout" else RuntimeError):
        asyncio.run(CodexCLICompleter(cfg, on_event=events.append)("System", "User"))
    assert events[-1]["type"] == "model_error"


def test_config_accepts_cli_and_rejects_invalid_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIM_MODEL_PROVIDER", "codex-cli")
    path = tmp_path / "config.toml"
    path.write_text('[model]\ncodex_model="test-model"\n[compile]\ncheck_concurrency=2\n')
    cfg = load_config(path)
    assert cfg.provider == "codex-cli" and cfg.model_name == "test-model"
    assert cfg.check_concurrency == 2
    path.write_text('[compile]\ncheck_concurrency=0\n')
    with pytest.raises(ValueError, match="check_concurrency"):
        load_config(path)


def test_cancelled_cli_is_reaped(tmp_path):
    pid_path = tmp_path / "pid"
    binary = fake_cli(tmp_path, f"import os,time\nopen({str(pid_path)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n")
    async def run():
        task = asyncio.create_task(CodexCLICompleter(Config(model_provider="codex-cli", codex_binary=binary))("System", "User"))
        for _ in range(200):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_path.exists()
        pid = int(pid_path.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    asyncio.run(run())
