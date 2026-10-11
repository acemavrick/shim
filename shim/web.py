"""The demo frontend: paste a URL, watch it compile.

One FastAPI app serving one static page. Compile progress reaches the browser
over server-sent events, which is why actions appear one at a time instead of
all at once when the run finishes.
"""

from __future__ import annotations

import asyncio
import json
import re
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
    return {"exploration_available": cfg.has_model, "model": cfg.model,
            "provider": "Anthropic", "runtime": "Python / Playwright Chromium",
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
        raise HTTPException(422, "Browser exploration requires an Anthropic API key in shim.config.toml.")
    explore = config().has_model if agent is None else agent
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            await compile_site(url, max_pages=max_pages, emit=emit, intent=intent,
                               agent=explore, allow_writes=allow_writes)
        except Exception as exc:
            await queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
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


@app.post("/api/call")
async def call(req: CallRequest) -> dict[str, Any]:
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
