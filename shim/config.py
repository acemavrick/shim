"""Configuration, resolved once per process.

Precedence, highest first:
  1. environment variables
  2. shim.config.toml in the working directory
  3. defaults below

Keys live in the TOML file, which is gitignored. Nothing here is required --
the compiler runs without an API key. At compile time a model can explore the
site and propose tools, descriptions and procedures. Runtime calls use HTTP only.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .safety import ReadPostRule

CONFIG_FILENAME = "shim.config.toml"
DEFAULT_MODEL = "claude-sonnet-5"


@dataclass
class Config:
    # Credentials
    anthropic_api_key: str | None = None

    # Model use at compile time (exploration, tools and descriptions)
    model: str = DEFAULT_MODEL
    use_model_naming: bool = True
    model_provider: str = "anthropic"
    codex_binary: str = "codex"
    codex_model: str | None = None
    model_timeout: float = 180.0
    codex_reasoning_effort: str = "low"
    agent_steps: int = 16

    # Crawl behaviour
    max_pages: int = 12
    min_request_gap: float = 0.35
    user_agent: str = "shim-compiler/0.1 (site-to-agent translation layer)"
    respect_robots: bool = True
    crawl_concurrency: int = 4
    check_concurrency: int = 4

    # Safety
    allow_writes: bool = False
    read_post_rules: list[ReadPostRule] = field(default_factory=list)

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_model(self) -> bool:
        """Whether model-assisted naming can run at all."""
        if not self.use_model_naming:
            return False
        return (bool(shutil.which(self.codex_binary)) if self.provider == "codex-cli"
                else bool(self.anthropic_api_key))

    @property
    def provider(self) -> str:
        if self.model_provider == "auto":
            return "anthropic" if self.anthropic_api_key else "codex-cli"
        return self.model_provider

    @property
    def model_name(self) -> str:
        if self.provider != "codex-cli":
            return self.model
        if self.codex_model:
            return self.codex_model
        # Reuse only the user's model selection, not their tools/hooks/instructions.
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        try:
            return str(_load_file(path).get("model") or "Codex CLI default")
        except SystemExit:
            return "Codex CLI default"

    def describe(self) -> str:
        key = "set" if self.anthropic_api_key else "not set"
        naming = "on" if self.has_model else "off (heuristic naming)"
        return (
            f"provider: {self.provider} | anthropic key: {key} | model: {self.model_name} | naming: {naming} | "
            f"max pages: {self.max_pages} | writes: "
            f"{'allowed' if self.allow_writes else 'blocked'}"
        )


def _load_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{path}: {exc}") from exc


def load_config(path: Path | None = None) -> Config:
    data = _load_file(path or Path(CONFIG_FILENAME))

    keys = data.get("keys", {}) if isinstance(data.get("keys"), dict) else {}
    compile_opts = data.get("compile", {}) if isinstance(data.get("compile"), dict) else {}
    model_opts = data.get("model", {}) if isinstance(data.get("model"), dict) else {}

    def pick(env: str, *values: Any, default: Any) -> Any:
        if os.environ.get(env):
            return os.environ[env]
        for value in values:
            if value is not None:
                return value
        return default

    config = Config(
        anthropic_api_key=pick(
            "ANTHROPIC_API_KEY", keys.get("anthropic"), default=None
        ),
        model=str(pick("SHIM_MODEL", model_opts.get("name"), default=DEFAULT_MODEL)),
        use_model_naming=bool(model_opts.get("naming", True)),
        model_provider=str(pick("SHIM_MODEL_PROVIDER", model_opts.get("provider"), default="anthropic")),
        codex_binary=str(pick("SHIM_CODEX_BINARY", model_opts.get("codex_binary"), default="codex")),
        codex_model=pick("SHIM_CODEX_MODEL", model_opts.get("codex_model"), default=None),
        model_timeout=float(pick("SHIM_MODEL_TIMEOUT", model_opts.get("timeout"), default=180)),
        codex_reasoning_effort=str(model_opts.get("codex_reasoning_effort", "low")),
        agent_steps=int(pick("SHIM_AGENT_STEPS", compile_opts.get("agent_steps"), default=16)),
        max_pages=int(pick("SHIM_MAX_PAGES", compile_opts.get("max_pages"), default=12)),
        min_request_gap=float(
            pick("SHIM_REQUEST_GAP", compile_opts.get("request_gap"), default=0.35)
        ),
        user_agent=str(
            pick(
                "SHIM_USER_AGENT", compile_opts.get("user_agent"),
                default=Config.user_agent,
            )
        ),
        respect_robots=bool(compile_opts.get("respect_robots", True)),
        crawl_concurrency=int(pick("SHIM_CRAWL_CONCURRENCY", compile_opts.get("crawl_concurrency"), default=4)),
        check_concurrency=int(pick("SHIM_CHECK_CONCURRENCY", compile_opts.get("check_concurrency"), default=4)),
        allow_writes=bool(compile_opts.get("allow_writes", False)),
        read_post_rules=[ReadPostRule.model_validate(r) for r in data.get("safety", {}).get("read_post_rules", [])],
        raw=data,
    )
    if config.min_request_gap < 0:
        raise ValueError("request_gap must be nonnegative")
    if config.model_provider not in {"anthropic", "codex-cli", "auto"}:
        raise ValueError("model provider must be anthropic, codex-cli, or auto")
    if config.model_timeout <= 0 or not 1 <= config.agent_steps <= 64:
        raise ValueError("model timeout must be positive; agent_steps must be between 1 and 64")
    if config.codex_reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("invalid codex_reasoning_effort")
    for name in ("crawl_concurrency", "check_concurrency"):
        if not 1 <= getattr(config, name) <= 16:
            raise ValueError(f"{name} must be between 1 and 16")
    return config


_cached: Config | None = None


def config() -> Config:
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
