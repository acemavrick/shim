"""Configuration, resolved once per process.

Precedence, highest first:
  1. environment variables
  2. shim.config.toml in the working directory
  3. defaults below

Keys live in the TOML file, which is gitignored. Nothing here is required --
the compiler runs fully without an API key, and a model is only ever used to
improve names and descriptions, never to execute a call.
"""

from __future__ import annotations

import os
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

    # Model use at compile time (naming and descriptions only)
    model: str = DEFAULT_MODEL
    use_model_naming: bool = True

    # Crawl behaviour
    max_pages: int = 12
    min_request_gap: float = 0.35
    user_agent: str = "shim-compiler/0.1 (site-to-agent translation layer)"
    respect_robots: bool = True

    # Safety
    allow_writes: bool = False
    read_post_rules: list[ReadPostRule] = field(default_factory=list)

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_model(self) -> bool:
        """Whether model-assisted naming can run at all."""
        return bool(self.anthropic_api_key) and self.use_model_naming

    def describe(self) -> str:
        key = "set" if self.anthropic_api_key else "not set"
        naming = "on" if self.has_model else "off (heuristic naming)"
        return (
            f"anthropic key: {key} | model: {self.model} | naming: {naming} | "
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
        allow_writes=bool(compile_opts.get("allow_writes", False)),
        read_post_rules=[ReadPostRule.model_validate(r) for r in data.get("safety", {}).get("read_post_rules", [])],
        raw=data,
    )
    return config


_cached: Config | None = None


def config() -> Config:
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
