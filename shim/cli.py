"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .compile import compile_site, load_manifest
from .runtime.http import close_client, invoke_action

TIER_MARK = {"read": "read", "reversible": "write", "irreversible": "DANGER"}


async def _compile(args: argparse.Namespace) -> int:
    def show(event: dict) -> None:
        kind = event.get("type")
        if kind == "stage":
            extra = {k: v for k, v in event.items() if k not in ("type", "stage")}
            if event["stage"] == "links":
                extra = {"patterns": [u.split("//", 1)[-1] for u in event["patterns"]][:6]}
            print(f"\n[{event['stage']}] {extra}", flush=True)
        elif kind == "page":
            print(f"  page  {event['url']}", flush=True)
        elif kind == "action":
            req = ",".join(event["required"])
            print(
                f"  TOOL  {event['name']}({req})  [{TIER_MARK[event['tier']]}]"
                f"  {event['items_found']} items",
                flush=True,
            )
        elif kind == "action_failed":
            print(f"  drop  {event['name']}  ({event['error']})", flush=True)
        elif kind == "agent_step":
            mark = "x" if event.get("failed") else ">"
            print(f"  agent {mark} {event['action']} {event['name']!r}  {event['why']}"
                  + (f"  [{event['failed']}]" if event.get("failed") else ""), flush=True)
        elif kind == "agent_done":
            print(f"  agent done: {event['steps']} step(s), {event['observed']} request(s) observed,"
                  f" proposed {event['tools']}\n        {event['summary']}", flush=True)
        elif kind == "agent_write_down":
            print(f"  saving interface from {event['pages']} page(s) and {event['requests']} request(s)", flush=True)
        elif kind == "agent_write_down_failed":
            print(f"  interface proposal failed: {event['error']}", flush=True)
        elif kind == "procedure":
            print(f"  HOWTO {event['intent']!r}: {event['steps']} step(s)", flush=True)
        elif kind == "describe_done":
            print(f"  model: {event['calls']} proposal(s), {event['failures']} failed", flush=True)
        elif kind == "records":
            print(f"  DATA  {event['count']} declared record(s): {', '.join(event['types'])}", flush=True)
        elif kind == "skip":
            print(f"  skip  {event['what']}  ({event['reason']})", flush=True)

    manifest = await compile_site(
        args.url,
        max_pages=args.max_pages,
        allow_writes=args.allow_writes,
        include_pages=args.pages,
        use_browser=args.browser,
        intent=args.intent,
        agent=args.agent,
        emit=show,
    )
    cov = manifest.coverage
    print(
        f"\n{cov.actions_shipped} action(s) shipped of {cov.actions_generated} found"
        f" across {cov.pages_crawled} page(s); {len(cov.skipped)} skipped"
        + (f"; {cov.records_extracted} declared record(s)" if cov.records_extracted else "")
    )
    print(f"manifest: sites/{manifest.host}/manifest.json")
    print(f"formats:  sites/{manifest.host}/agent/  "
          f"(openapi.json, llms.txt, webmcp.js, calls.sh, index.html)")
    return 0


async def _emit_cmd(args: argparse.Namespace) -> int:
    from .emit import emit_all
    from .compile import manifest_path

    manifest = load_manifest(args.host)
    written = emit_all(manifest, manifest_path(args.host).parent / "agent")
    for path in written:
        print(f"  {path}")
    return 0


async def _call(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.host)
    action = next((a for a in manifest.actions if a.name == args.action), None)
    if action is None:
        names = ", ".join(a.name for a in manifest.actions) or "(none)"
        print(f"no action {args.action!r}. Available: {names}", file=sys.stderr)
        return 1

    payload = json.loads(args.json) if args.json else {}
    if action.tier == "irreversible" and not args.confirm:
        print(
            f"{action.name} is irreversible. Re-run with --confirm to execute.",
            file=sys.stderr,
        )
        return 2

    result = await invoke_action(action, payload)
    print(json.dumps({
        "action": action.name,
        "status": result.status,
        "items_found": result.items_found,
        "bytes": result.bytes,
        "records": result.records[:10],
    }, indent=2))
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import run_stdio
    run_stdio(args.host)
    return 0


def _web(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("shim.web:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shim",
        description="Compile a website into typed MCP tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile", help="crawl a URL and write a manifest")
    p_compile.add_argument("url")
    p_compile.add_argument("--max-pages", type=int, default=12)
    p_compile.add_argument(
        "--intent", help='what an agent should be able to do here, e.g. "find a book by title"')
    p_compile.add_argument(
        "--agent", action=argparse.BooleanOptionalAction, default=None,
        help="browser exploration (automatic with a model key; --no-agent uses crawl only)")
    p_compile.add_argument(
        "--browser", action="store_true",
        help="render with Playwright and capture JSON endpoints (Tier 3)",
    )
    p_compile.add_argument("--pages", action="store_true", help="include page records")
    p_compile.add_argument(
        "--allow-writes", action="store_true",
        help="verify non-read actions (only for sites you own)",
    )

    p_call = sub.add_parser("call", help="call one action from the terminal")
    p_call.add_argument("host")
    p_call.add_argument("action")
    p_call.add_argument("json", nargs="?", default="{}")
    p_call.add_argument("--confirm", action="store_true")

    p_emit = sub.add_parser("emit", help="re-publish every agent format")
    p_emit.add_argument("host")

    sub.add_parser("config", help="show resolved configuration")

    p_serve = sub.add_parser("serve", help="start the MCP server over stdio")
    p_serve.add_argument("host")

    p_web = sub.add_parser("web", help="start the demo frontend")
    p_web.add_argument("--port", type=int, default=8808)

    args = parser.parse_args(argv)

    if args.command == "config":
        from .config import CONFIG_FILENAME, config
        print(config().describe())
        print(f"file: {CONFIG_FILENAME} (copy shim.config.example.toml to start)")
        return 0
    if args.command == "serve":
        return _serve(args)
    if args.command == "web":
        return _web(args)

    handler = {"compile": _compile, "call": _call, "emit": _emit_cmd}[args.command]

    async def run() -> int:
        try:
            return await handler(args)
        finally:
            await close_client()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
