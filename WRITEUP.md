# shim: a compiler from websites to agent tools

shim takes a website URL and produces an agent version of that site: a set of typed, verified tools an AI agent can call, plus instructions for the parts that cannot be turned into a request. It is published in every format an agent might read (MCP, OpenAPI, llms.txt, WebMCP, curl) and served without any model in the loop. A model explores the site once at compile time. After that, every call is a deterministic HTTP replay.

## The problem

Websites were built for people. An agent that needs to search a bookstore, look up a product, or add something to a cart has to rediscover that site every session by reading HTML and guessing at forms. That rediscovery is slow, expensive, fragile, and repeated by every agent that visits.

The thesis of shim is that this should happen once. One exploration, with a stated intent, by a model driving a real browser. What it observes becomes verified tools where a replayable request exists and written instructions where it does not. The result is saved and published so no agent has to learn the site again.

## How it works

```
url + intent
   |
   +-- crawl (HTTP, robots-aware) --> forms, link patterns, JSON-LD records
   |
   +-- agent (Playwright + model) --> label-based steps + every request the site made
   |
   v
candidates --> check: fire each one live, verify the extractor --> manifest.json
   |
   v
MCP tools + prompts, openapi.json, llms.txt, webmcp.js, calls.sh, /.agent page
```

### 1. Explore

Two explorers run at the same time.

The crawler fetches pages over plain HTTP, breadth-first, same-site, respecting robots.txt. From each page it reads three kinds of evidence: `<form>` elements (which declare their own method, action and field names), link patterns such as `/ebooks/1342` and `/ebooks/84` that reveal a detail-page tool, and JSON-LD records the site declares about itself.

The agent is a model driving headless Chromium through the page's accessibility tree, the way a browser extension would. Each turn it sees the current page, the same-site links, and the requests the site made since its last action, and replies with one action: click, fill, press, goto, scroll, back, or a bounded read-only inspection of the DOM, links, network, or console. Every step and every request is recorded. The browser sends requests the way a visitor would; only paths that look like checkout, orders, payment or deletion, and bodies carrying credentials, are blocked unless writes are explicitly allowed.

### 2. Translate

Each kind of evidence becomes candidate actions through a different path:

- A form becomes one action per branching submit button. A visible named control is an agent parameter; a hidden field is a constant pinned into the request. Selects and radios become enums.
- A link cluster with one varying segment becomes a detail tool with the observed values as examples.
- JSON-LD records are kept as declared data with no inference at all.
- The agent's session goes through a write-down call: the model is shown the steps it took, the requests the site made with response samples, and the rendered pages, and proposes tools, a procedure, a site description, and notes.

A proposal from the model is treated as untrusted. A tool must match a request the site was actually observed making. Example values must have been sent to the site, and ones that were not are pruned. Session tokens, cookies and API keys can never be pinned as constants. A URL the site answered with a redirect is resolved to the document that actually answered. Proposals are validated against the whole session, not just the slice of evidence the model was shown.

### 3. Check

This is the stage that makes the output trustworthy. Every candidate is fired at the live site once before it ships. A form's declared method and action are fiction if the page intercepts submission in JavaScript, and the only way to find out is to send the request.

For a read with a free-text input, the extractor is chosen by diffing two responses. Two different queries are sent, and whatever changed between the two answers is the result set; whatever stayed the same is navigation. An extractor that returns identical records for two different queries is reading page furniture and is rejected. A tool with a single observed example is verified on that one request. Writes pass on a 2xx and are never fired at a site the compiler does not own unless `--allow-writes` is passed.

The rule is simple: no `passed`, no tool. A failed candidate is absent from the tool list entirely, with its reason recorded in the manifest's coverage section.

### 4. Publish and serve

The manifest is the contract between compiler and server. From it, shim emits OpenAPI 3.1, llms.txt, a WebMCP registration script, a runnable shell script of curl calls, a human-readable /.agent page, and the readable content of each page as markdown. The MCP server reads the manifest and nothing else: it fills the request template, sends it, runs the extractor, and returns JSON. Procedures become MCP prompts.

The check and the server share one runtime module. A template that passes the check and fails at serve time is the exact bug that split exists to prevent.

## Two invariants

1. No model runs at call time. Models propose names, extractors, risk tiers, procedures and exploration steps at compile time. The compiler verifies, the server replays.
2. The agent proposes, the compiler verifies. Every tool is fired against the live site before it ships. A model may raise a tool's risk tier, never lower it.

## Safety model

Every tool carries a tier. GET is always `read`. POST is `reversible` by default and escalated to `irreversible` by danger words in the submit text, heading or path. Irreversible tools take a `confirm` parameter and refuse without it. A POST ships as a read tool only when the explorer observed the site make that request, the model named it as a read, and two inputs returned different records. The manifest is the authorization at serve time.

## Tech

| Layer | Choice |
|---|---|
| Language | Python 3.12+, asyncio throughout |
| HTTP | httpx async client, per-origin request pacing, shared cookie jar |
| HTML | BeautifulSoup with lxml and soupsieve; selectors compiled once and cached |
| Browser | Playwright 1.63 driving headless Chromium; accessibility-tree snapshots, request routing, network capture |
| Contract | Pydantic v2 models in `shim/manifest.py`; every module derives its types from it |
| Models | Anthropic SDK with `claude-sonnet-5` by default, or the Codex CLI as a text-only completer; compile time only |
| Serving | MCP Python SDK over stdio; tools from the manifest, procedures as prompts |
| Demo UI | FastAPI and uvicorn, progress streamed over server-sent events, a per-run registry so verified tools are callable before the compile finishes |
| Storage | `sites/<host>/manifest.json`, every published format under `agent/`, immutable versioned runs under `history/` |
| Tests | pytest, 211 tests, no network |

## What it does on real sites

| Site | Kind | Shipped |
|---|---|---|
| randeria.dev | static portfolio | `list_projects()`, `get_project_detail(slug)` |
| demoblaze.com | JavaScript store with a POST JSON API | `list_products()`, `list_products_by_category(cat)` as a verified POST read |
| gutenberg.org | server-rendered | `search_books(query)`, `get_ebook(id)` with a model-chosen extractor |
| en.wikipedia.org | server-rendered | search tools, a REST title search, article fetch, declared JSON-LD records |
| argoshome.bg | Shopify store | collection listings with filters, product detail, product JSON |

Every shipped tool was executed against the live site before publication, and calling it afterwards from the saved manifest runs no model.

## What was hard

The observation is the easy part. The verification layer is where the real complexity lives, and every rule in it was found on a real site: a session token the model exposed as an input, a client-minted cart id that must be generated at call time, POSTs that 403 without Origin and Referer, writes that return a confirmation instead of records, Tailwind class names that crash CSS selectors, navigation bars that are not `<nav>`, redirects that hide the real document, and a model that lists every example it saw while the validator was shown only a slice.

## Known limits

Sites behind CAPTCHAs, bot detection or login walls are detected and reported, not retried. Rotating CSRF tokens are captured at compile time and will be stale at call time; the refetch mechanism exists in the schema but is not yet wired to form parsing. OpenAPI and curl exports do not yet carry path parameters, constants and JSON bodies with the same fidelity as the MCP server. The check verifies each request in isolation, so a site can ship overlapping tools; a site-level coherence pass is the next step.

## Run it

```bash
uv sync --group dev
cp shim.config.example.toml shim.config.toml      # add an Anthropic key under [keys]

uv run python -m shim.cli compile https://www.demoblaze.com \
    --intent "find a laptop and add it to the cart"
uv run python -m shim.cli call www.demoblaze.com list_products_by_category '{"cat":"notebook"}'
uv run python -m shim.cli serve www.demoblaze.com    # MCP over stdio
uv run python -m shim.cli web                        # demo UI on :8808
uv run python -m pytest tests -q
```
