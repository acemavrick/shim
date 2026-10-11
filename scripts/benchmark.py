"""Repeatable end-to-end benchmark; only contacts a temporary local website.

Run from the repository root: uv run python -m scripts.benchmark --runs 3
Runtime extraction and overlapping calls: add --runtime, or --runtime-only.
"""

import argparse
import asyncio
import json
import statistics
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from shim import compile as compiler
from shim.config import Config
from shim import config
from shim.runtime.http import close_client, invoke_action
from shim.runtime import http as runtime_http
from shim.runtime.extract import run_extract
from shim.manifest import Action, Extract, HttpBackend, InputSchema


class Site(BaseHTTPRequestHandler):
    latency = 0.6
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        time.sleep(self.latency)
        path = urlsplit(self.path).path
        content_type = "text/html"
        if path == "/robots.txt":
            body = "User-agent: *\nAllow: /\n"
            content_type = "text/plain"
        elif path.startswith("/lookup"):
            content_type = "application/json"
            body = json.dumps({"items": [{"name": f"River {path}", "price": 12},
                                         {"name": f"Forest {path}", "price": 25}]})
        elif path == "/":
            links = "".join(f'<li><a href="/entries/{i}">Project {i}</a></li>' for i in range(11))
            forms = "".join(f'<form action="/lookup{i}" method="GET"><input type="hidden" name="collection" value="{i}"><button>List collection {i}</button></form>'
                            for i in range(8))
            body = f"<html><title>Benchmark</title><main><h1>Projects</h1><ul>{links}</ul>{forms}</main></html>"
        else:
            body = f"<html><title>Project {path}</title><main><h1>Project {path}</h1><p>Engineering project details.</p></main></html>"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RuntimeSite(BaseHTTPRequestHandler):
    """Independent fixture: runtime calls never change compile request counts."""

    latency = 0.05
    requests = 0
    lock = threading.Lock()

    def do_GET(self):
        with type(self).lock:
            type(self).requests += 1
            generation = type(self).requests
        time.sleep(self.latency)
        content_type = "application/json"
        if urlsplit(self.path).path == "/form":
            content_type = "text/html"
            data = ("<html><body>" + "".join(
                f'<section><h2>Product {i}</h2><p>details</p><a href="/item/{i}">view</a></section>'
                for i in range(10000))
                + f'<form><input type="hidden" name="csrf" value="token-{generation}"></form></body></html>').encode()
        else:
            data = json.dumps({"generation": generation, "enabled": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


async def benchmark_runtime(args):
    html = "<ul>" + "".join(
        f'<li class="entry" data-id="{i}"><a class="name" href="/item/{i}"> Item {i} </a>'
        '<span class="price">$12.50</span></li>' for i in range(2000)) + "</ul>"
    body = json.dumps({"items": [
        {"id": i, "meta": {"title": f"Item {i}"}, "prices": [{"cost": "12.50"}],
         "enabled": False, "none": None} for i in range(20000)]})
    fixtures = [
        ("css_2000_rows_4_fields", html, Extract(items=".entry", fields={
            "id": "@data-id", "name": ".name", "url": ".name@href", "price": ".price|number"}), 2000),
        ("json_20000_rows_5_fields", body, Extract(kind="json", items="items", fields={
            "id": "id", "name": "meta.title", "price": "prices.0.cost|number",
            "enabled": "enabled", "none": "none"}), 20000),
    ]
    for name, content, extract, rows in fixtures:
        # Warm only reusable selector/path metadata; response data is always parsed.
        run_extract(content, extract, "https://benchmark.test/")
        samples = []
        for _ in range(args.runs):
            started = time.perf_counter()
            result = run_extract(content, extract, "https://benchmark.test/")
            samples.append((time.perf_counter() - started) * 1000)
            assert result.items_found == rows
        print(json.dumps({"scenario": name, "median_extract_ms": round(statistics.median(samples), 3),
                          "rows": rows, "runs": args.runs}), flush=True)

    RuntimeSite.latency = args.latency
    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    action = Action(name="fresh_generation", description="Read a new response", tier="read",
                    input=InputSchema(), extract=Extract(kind="json", fields={"generation": "generation"}),
                    backend=HttpBackend(method="GET", url=f"http://127.0.0.1:{server.server_port}/"))
    config._cached = Config(min_request_gap=args.gap)
    calls = 8
    try:
        samples = []
        tokens = set()
        for _ in range(args.runs):
            started = time.perf_counter()
            fields = await runtime_http._refetch_hidden_fields(f"http://127.0.0.1:{server.server_port}/form")
            samples.append((time.perf_counter() - started) * 1000)
            tokens.add(fields["csrf"])
        assert len(tokens) == args.runs
        print(json.dumps({"scenario": "hidden_form_refetch_10000_sections", "latency_s": args.latency,
                          "median_refetch_ms": round(statistics.median(samples), 3), "runs": args.runs}), flush=True)
        for concurrency in [1, args.concurrency]:
            samples = []
            for _ in range(args.runs):
                semaphore = asyncio.Semaphore(concurrency)
                async def call():
                    async with semaphore:
                        return await invoke_action(action, {})
                before = RuntimeSite.requests
                started = time.perf_counter()
                results = await asyncio.gather(*(call() for _ in range(calls)))
                samples.append(time.perf_counter() - started)
                assert RuntimeSite.requests - before == calls
                assert len({r.records[0]["generation"] for r in results}) == calls
            print(json.dumps({"scenario": "independent_runtime_calls", "calls": calls,
                              "concurrency": concurrency, "latency_s": args.latency, "request_gap_s": args.gap,
                              "median_calls_s": round(statistics.median(samples), 3)}), flush=True)
    finally:
        await close_client()
        server.shutdown()
        server.server_close()


async def benchmark(args):
    Site.latency = args.latency
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    config._cached = Config(min_request_gap=args.gap)
    # Assignment also works against the original revision for before/after measurements.
    config._cached.crawl_concurrency = args.concurrency
    config._cached.check_concurrency = args.concurrency
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="shim-benchmark-") as tmp:
            compiler.SITES_DIR = Path(tmp)
            for _ in range(args.runs):
                phases = {}
                first_candidate = first_verified = None
                stage, stage_at = "startup", time.perf_counter()
                def emit(event):
                    nonlocal stage, stage_at, first_candidate, first_verified
                    now = time.perf_counter()
                    if event['type'] == 'tool_candidate' and first_candidate is None:
                        first_candidate = round(now - started, 3)
                    if event['type'] == 'action' and first_verified is None:
                        first_verified = round(now - started, 3)
                    if event['type'] == 'done' and 'timings_ms' in event:
                        phases.update({k: round(v / 1000, 3) for k, v in event['timings_ms'].items()})
                        return
                    if event["type"] in {"stage", "done"}:
                        now = time.perf_counter()
                        phases[stage] = round(now - stage_at, 3)
                        stage = event.get("stage", "done")
                        stage_at = now
                before = Site.requests
                started = time.perf_counter()
                manifest = await compiler.compile_site(url, max_pages=12, agent=False, emit=emit)
                elapsed = time.perf_counter() - started
                assert len(manifest.pages) == 12
                assert len(manifest.actions) == 9, manifest.coverage
                call_at = time.perf_counter()
                called = await invoke_action(manifest.actions[0], {})
                assert called.status == 200 and called.items_found == 2
                result = {"compile_s": round(elapsed, 3), "call_s": round(time.perf_counter() - call_at, 3),
                          "pages": len(manifest.pages), "tools": len(manifest.actions),
                          "requests": Site.requests - before, "phases_s": phases,
                          "first_candidate_s": first_candidate, "first_verified_s": first_verified}
                results.append(result)
                print(json.dumps(result), flush=True)
                await close_client()
    finally:
        await close_client()
        server.shutdown()
        server.server_close()
    print(json.dumps({"median_compile_s": round(statistics.median(r["compile_s"] for r in results), 3),
                      "concurrency": args.concurrency, "latency_s": args.latency, "request_gap_s": args.gap}), flush=True)


async def main(args):
    if not args.runtime_only:
        await benchmark(args)
    if args.runtime or args.runtime_only:
        await benchmark_runtime(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--latency", type=float, default=0.6)
    parser.add_argument("--gap", type=float, default=0.35)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--runtime", action="store_true", help="Also benchmark extraction and independent calls")
    parser.add_argument("--runtime-only", action="store_true", help="Skip compile and benchmark runtime only")
    parsed = parser.parse_args()
    if parsed.runs < 1 or parsed.concurrency < 1 or parsed.latency < 0 or parsed.gap < 0:
        parser.error("runs/concurrency must be positive and latency/gap nonnegative")
    asyncio.run(main(parsed))
