# Shim

Shim compiles a website into a saved interface for future agents. It crawls HTML,
optionally explores the site with a model and Chromium, infers request tools,
verifies them against live responses, and saves typed tools, readable content,
browser instructions and procedures. Calling a saved tool uses HTTP and extraction;
it never starts a model. A verified browser recipe starts a fresh, read-only Chromium session.

## Run locally

```sh
uv sync --dev
uv run playwright install chromium
cp shim.config.example.toml shim.config.toml
uv run python -m shim.cli web --port 8808
```

Open <http://127.0.0.1:8808>. The local configuration is gitignored.

## Choose a model backend

**Anthropic API key:** keep `[model] provider = "anthropic"`, configure a model
available to your account in `name`, and set `ANTHROPIC_API_KEY` or
`[keys] anthropic` in the local TOML file.

**Local Codex CLI:** run `codex login`, then configure:

```toml
[model]
provider = "codex-cli"
codex_binary = "codex"
codex_reasoning_effort = "low"
timeout = 180
# codex_model = "an account-supported model"
```

The local Homebrew CLI was upgraded from `0.142.5` to `0.162.1` and validated
with its existing ChatGPT login. It now works with `codex_binary = "codex"`.
If another machine's desktop app has a newer working CLI than its shell, use
that executable's absolute path instead.

Shim reuses CLI login authentication. If `codex_model` is omitted, it reads only
the model name from the local Codex configuration. Each completion runs in a
temporary directory with read-only sandboxing, disabled shell/browser/plugin
tools, compact Shim instructions, a response timeout and cancellation cleanup. Shim operates the browser;
the CLI receives evidence and returns JSON decisions. CLI authentication is local;
model inference still uses the configured remote service.

`provider = "auto"` prefers a configured Anthropic key, then a CLI executable.
Availability detects the key or executable; actual authentication/model errors
appear in compile telemetry. Use `SHIM_MODEL_PROVIDER` to override the backend
without rewriting TOML. Other overrides: `SHIM_CODEX_BINARY`, `SHIM_CODEX_MODEL`,
`SHIM_MODEL`, `SHIM_MODEL_TIMEOUT` and `SHIM_AGENT_STEPS`.

```sh
uv run python -m shim.cli config
uv run python -m shim.cli compile https://www.befreed.ai --max-pages 2 --agent \
  --intent "Learn the public book catalog and inspect a representative book"
uv run python -m shim.cli call www.befreed.ai <verified_action> '{"slug":"observed-slug"}'
uv run python -m shim.cli serve www.befreed.ai
```

Use `--no-agent` for deterministic compilation. `--browser --no-agent` renders
JavaScript without model decisions. Saved outputs are in `sites/<host>/`:
`manifest.json`, `agent/openapi.json`, `agent/llms.txt`, `agent/webmcp.js`,
`agent/calls.sh`, `agent/index.html`, readable pages and notes.

Completed compilations also live in immutable `sites/<host>/history/<run_id>/`
directories. The UI can reload an earlier version and binds its calls/files to
that version. Live streaming uses a separate, expiring registry ID; once saved,
tools no longer depend on that registry. Read-only browser recipes run only
through MCP/Python, not the direct HTTP exports. During exploration the browser
sends POSTs like a visitor would, except checkout/order/payment/delete paths and
credential fields, which stay blocked without `--allow-writes`. A POST ships as a
read tool only when the explorer observed the site make it, the model named it
as a read, and two inputs returned different records. `[[safety.read_post_rules]]`
remains as an escape hatch for a lookup the explorer could not observe.

## Performance

HTTP crawling and the read-only browser agent begin together. Forms and link
patterns become visible as soon as a page arrives, and independent GET checks
start during the crawl, including while the agent is still exploring. Rendered
browser pages feed discovery immediately. Safe www redirects establish the crawl's
canonical host, so entering `befreed.ai` also discovers `www.befreed.ai` links.
The UI updates each candidate to checking, verified or failed; verified
tools are callable immediately through a per-run server registry, before the
manifest is saved. Candidates cannot be invoked. Browser state reads and safe
primary/control GET probes overlap. Verification continuously refills available
slots instead of waiting for a complete batch. Output order remains deterministic; writes,
cookie-dependent actions and rotating-token requests remain sequential barriers.
Requests to each origin retain a configurable start gap, while unrelated origins
can proceed independently. No response cache is used, so runtime calls remain fresh.
JSON field specifications and paths are parsed once and cached, rather than per record.
HTML selectors are compiled once; fields sharing a node reuse it within each record.
Valid agent proposals reuse their extractors after fresh HTTP verification, avoiding
redundant model proposals. A richer proposal replacing a provisional tool must
pass its own verification; one malformed proposal cannot discard valid siblings.
Failed identical inputs are checked once instead of
repeating the same request and model prompt for each crawl-derived probe word.
CLI/UI telemetry reports phase times, model time and actual token usage.

```toml
[compile]
crawl_concurrency = 4
check_concurrency = 4
request_gap = 0.35
agent_steps = 16
```

Set concurrency to `1` to compare serial behavior. Keep the gap appropriate for
the target website; use a zero gap only for a local or controlled benchmark.

```sh
uv run python -m scripts.benchmark --runs 3
uv run python -m scripts.benchmark --runtime-only --runs 5 --latency 0.05 --gap 0.005
uv run pytest -q
```

Measured on this Mac against original revision `b6dd389`; raw results are
recorded in `reports/performance.json`. The local HTTP benchmark
uses 600 ms server latency, a 350 ms request gap, 12 pages, 9 verified tools and
24 requests including one runtime call, without model calls. Median compilation
fell from **14.083 s to 8.906 s** (36.8% less time, 1.58× throughput).
The first verified tool appeared after **1.861 s** (median), while crawling continued. A single
network-bound call remained about **0.608 s**. Extracting 20,000 JSON records with
five fields, including JSON decoding, fell from **60.119 ms to 28.367 ms** (2.12× faster).
HTML extraction of 2,000 rows with four fields fell from **78.845 ms to 58.651 ms**.
Eight fresh calls on a controlled 50 ms server with a 5 ms gap took **0.468 s** serially
and **0.132 s** with concurrency four on the optimized build. These measurements
do not predict arbitrary websites or full model-assisted exploration latency.

An earlier four-decision live Codex CLI run on befreed.ai showed its first
candidate after 1.476 s and finished in 83.503 s with three verified tools.
A book detail call returned HTTP 200 and one record in 0.254 s. An earlier run
shipped five tools in about 103 s and passed a real MCP call in 0.239 s. Because
model exploration chose different capabilities, those total times are not an
equal-coverage performance comparison. Model responses still dominate a live compile.
The actual streaming UI also passed a local fixture test with CLI naming enabled:
its first callable tool appeared at 6.864 s and was invoked before the 25.746 s
compile completed. This is a separate test from the model-free benchmark. The
Anthropic path passes mocked SDK/compile tests; a live Anthropic call still needs
a real credential. befreed.ai's public pages render successfully. Wikipedia returned HTTP bot
protection in the HTTP trial; the compiler records that limitation and stops.

See [OpenAI's non-interactive CLI documentation](https://learn.chatgpt.com/docs/developer-commands#codex-exec)
for the CLI invocation and JSON event format.

## Demo video

[Watch the 35-second BeFreed demo](reports/demo/shim-befreed-demo.mp4), with short English
captions and a light instrumental soundtrack. It shows the actual website, an explicitly labelled replay
of real compile events, a saved tool schema, a fresh HTTP 200 call, and exported
interfaces. [Script and reproduction notes](reports/demo/README.md) are included.
