"""The demo frontend: paste a URL, watch it compile.

One FastAPI app serving one static page. Compile progress reaches the browser
over server-sent events, which is why actions appear one at a time instead of
all at once when the run finishes.
"""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from uuid import uuid4
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .compile import SITES_DIR, compile_site, load_manifest
from .config import config
from .manifest import Action
from .runtime.http import invoke_action
from .server import servable

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="shim", docs_url=None, redoc_url=None)

# Per-run verified definitions make completed checks callable while discovery
# continues. Never accept a tool definition supplied by the browser.
LIVE_RUNS: dict[str, dict[str, Any]] = {}
LIVE_TTL = 1800
LIVE_LIMIT = 32


def prune_live_runs():
    now = monotonic()
    for run_id, run in list(LIVE_RUNS.items()):
        if run["finished"] is not None and now - run["finished"] > LIVE_TTL:
            del LIVE_RUNS[run_id]
    completed = sorted((run["finished"], run_id) for run_id, run in LIVE_RUNS.items()
                       if run["finished"] is not None)
    for _, run_id in completed[:max(0, len(LIVE_RUNS) - LIVE_LIMIT + 1)]:
        del LIVE_RUNS[run_id]


def saved_manifest(host: str, run_id: str | None = None):
    if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", host) or host in {".", ".."}:
        raise HTTPException(422, "Invalid site host")
    try:
        if run_id:
            from .history import load_version
            return load_version(host, run_id)
        return load_manifest(host)
    except FileNotFoundError:
        raise HTTPException(404, "No saved site found. Compile this website first.")
    except ValueError:
        raise HTTPException(422, "Invalid compilation version")


@app.get("/api/config")
async def capabilities() -> dict[str, Any]:
    cfg = config()
    return {"exploration_available": cfg.has_model, "model": cfg.model_name,
            "provider": "Codex CLI" if cfg.provider == "codex-cli" else "Anthropic", "runtime": "Python / Playwright Chromium",
            "inspection_tools": ["dom", "links", "network", "console"]}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/sites")
async def sites() -> dict[str, Any]:
    """Previously compiled sites, so a dead Wi-Fi link cannot kill the demo."""
    out = []
    for path in sorted(SITES_DIR.glob("*/manifest.json")):
        try:
            manifest = load_manifest(path.parent.name)
        except Exception:
            continue
        output = path.parent / "agent"
        if manifest.run_id:
            from .history import version_dir
            output = version_dir(manifest.host, manifest.run_id) / "agent"
        out.append({
            "host": manifest.host,
            "site": manifest.site,
            "compiled_at": manifest.compiled_at,
            "run_id": manifest.run_id,
            "actions": len(servable(manifest)),
            "intent": manifest.intent,
            "procedures": len(manifest.procedures),
            "records": sum(len(p.records) for p in manifest.pages),
            "formats": sorted(p.name for p in output.glob("*") if p.is_file()),
        })
    return {"sites": out}


@app.get("/api/compile")
async def compile_stream(url: str, max_pages: int = Query(10, ge=1, le=50), intent: str | None = None,
                         agent: bool | None = None, allow_writes: bool = False) -> StreamingResponse:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise HTTPException(422, "Enter an HTTP or HTTPS website URL without credentials.")
    intent = intent.strip() if intent else None
    if agent and not config().has_model:
        raise HTTPException(422, "Configure an Anthropic API key or the Codex CLI provider in shim.config.toml.")
    explore = config().has_model if agent is None else agent
    prune_live_runs()
    if sum(run["finished"] is None for run in LIVE_RUNS.values()) >= LIVE_LIMIT:
        raise HTTPException(429, "Too many active compile runs")
    run_id = uuid4().hex
    live = {"host": parts.netloc, "actions": {}, "finished": None}
    LIVE_RUNS[run_id] = live
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        if event.get("type") == "run":
            live["version"] = event["run_id"]
        candidate_id = event.get("candidate_id")
        if candidate_id and event.get("type") == "action":
            action = Action.model_validate(event["definition"])
            if action.check is not None and action.check.passed:
                live["actions"][candidate_id] = action.model_copy(deep=True)
        elif candidate_id and event.get("type") in {"action_failed", "tool_checking"}:
            live["actions"].pop(candidate_id, None)
        await queue.put({**event, "run_id": live.get("version", run_id), "live_id": run_id, "host": parts.netloc})

    async def run() -> None:
        try:
            await compile_site(url, max_pages=max_pages, emit=emit, intent=intent,
                               agent=explore, allow_writes=allow_writes)
        except Exception as exc:
            await queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            live["finished"] = monotonic()
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class CallRequest(BaseModel):
    host: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False
    run_id: str | None = None
    live_id: str | None = None
    candidate_id: str | None = None


@app.post("/api/call")
async def call(req: CallRequest) -> dict[str, Any]:
    live_id = req.live_id or (req.run_id if req.run_id in LIVE_RUNS or (req.candidate_id and req.run_id) else None)
    if live_id is not None:
        prune_live_runs()
        live = LIVE_RUNS.get(live_id)
        if live is None:
            raise HTTPException(410, "This live run expired. Load the saved site to call its tools.")
        if live["host"] != req.host:
            raise HTTPException(404, "No live run for this site")
        if req.candidate_id is not None:
            action = live["actions"].get(req.candidate_id)
        else:
            matches = [a for a in live["actions"].values() if a.name == req.action]
            if len(matches) > 1:
                raise HTTPException(409, "Multiple live candidates share this name. Supply candidate_id.")
            action = matches[0] if matches else None
    else:
        manifest = saved_manifest(req.host, req.run_id)
        action = next((a for a in servable(manifest) if a.name == req.action), None)
    if action is None:
        raise HTTPException(404, f"No verified action {req.action!r}")

    validate_arguments(action, req.arguments)

    if action.tier == "irreversible" and not req.confirm:
        return {
            "status": "confirmation_required",
            "action": action.name,
            "message": "This action cannot be undone. Confirm to execute.",
        }

    arguments = {k: v.default for k, v in action.input.properties.items() if v.default is not None}
    arguments.update(req.arguments)
    try:
        result = await invoke_action(action, arguments)
    except (httpx.HTTPError, ValueError, NotImplementedError, TimeoutError) as exc:
        raise HTTPException(502, f"The saved tool could not run: {exc}")
    return {
        "action": action.name,
        "tier": action.tier,
        "status": result.status,
        "items_found": result.items_found,
        "bytes": result.bytes,
        "records": result.records[:12],
        "source": result.url,
        "blocked": result.blocked,
    }


def validate_arguments(action: Action, arguments: dict[str, Any]) -> None:
    errors = []
    for name in action.input.required:
        if name not in arguments or arguments[name] is None or arguments[name] == "":
            errors.append(f"{name} is required")
    for name, value in arguments.items():
        prop = action.input.properties.get(name)
        if prop is None:
            errors.append(f"Unknown input: {name}")
            continue
        valid = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
                 "integer": type(value) is int, "number": type(value) in (int, float)}[prop.type]
        if not valid:
            errors.append(f"{name} must be a {prop.type}")
            continue
        if prop.enum and value not in prop.enum:
            errors.append(f"{name} must be one of {', '.join(prop.enum)}")
        if isinstance(value, str):
            if prop.maxLength is not None and len(value) > prop.maxLength:
                errors.append(f"{name} is too long")
            if prop.pattern and not re.search(prop.pattern, value):
                errors.append(f"{name} does not match its required format")
        if type(value) in (int, float):
            if prop.minimum is not None and value < prop.minimum:
                errors.append(f"{name} must be at least {prop.minimum}")
            if prop.maximum is not None and value > prop.maximum:
                errors.append(f"{name} must be at most {prop.maximum}")
    if errors:
        raise HTTPException(422, "; ".join(errors))


@app.get("/agent/{host}/{filename}")
async def agent_file(host: str, filename: str, run_id: str | None = None) -> FileResponse:
    """Serves the emitted formats, as the site itself would publish them."""
    manifest = saved_manifest(host, run_id)
    safe = Path(filename).name  # no traversal
    path = SITES_DIR / host / "agent" / safe
    if manifest.run_id:
        from .history import version_dir
        path = version_dir(host, manifest.run_id) / "agent" / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no {safe} for {host}")
    types = {
        ".json": "application/json", ".txt": "text/plain; charset=utf-8",
        ".js": "application/javascript", ".sh": "text/x-shellscript",
        ".html": "text/html; charset=utf-8",
    }
    return FileResponse(path, media_type=types.get(path.suffix, "text/plain"))


@app.get("/api/manifest/{host}")
async def manifest(host: str, run_id: str | None = None) -> dict[str, Any]:
    m = saved_manifest(host, run_id)
    return json.loads(m.to_json())


@app.get("/api/history/{host}")
async def history(host: str):
    saved_manifest(host)
    from .history import list_versions
    return {"versions": list_versions(host)}
