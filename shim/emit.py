"""Emits the compiled site in every format an agent might speak.

The manifest is our internal contract, but no agent should have to learn it.
The same compiled knowledge is published as MCP tools (via `server.py`),
an OpenAPI document, an llms.txt file, a human-and-agent readable /.agent
page, a WebMCP registration script, and copy-pasteable shell calls.

One compile, many front doors. That is the point of the project: the unit of
output is an agent-ready *site*, not a single operation.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .manifest import Action, Manifest
from .server import servable

TIER_NOTE = {
    "read": "Safe to call freely; does not change anything.",
    "reversible": "Changes state but can be undone.",
    "irreversible": "Cannot be undone. Requires confirm=true.",
}


def _query_params(action: Action) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    for name, prop in action.input.properties.items():
        schema: dict[str, Any] = {"type": prop.type}
        if prop.enum:
            schema["enum"] = prop.enum
        if prop.pattern:
            schema["pattern"] = prop.pattern
        if prop.maxLength:
            schema["maxLength"] = prop.maxLength
        params.append({
            "name": name,
            "in": "query",
            "required": name in action.input.required,
            "description": prop.description or "",
            "schema": schema,
        })
    return params


def _record_schema(action: Action) -> dict[str, Any]:
    properties = {
        field: {"type": "number" if spec.endswith("|number") else "string"}
        for field, spec in action.extract.fields.items()
    }
    return {
        "type": "array",
        "items": {"type": "object", "properties": properties},
    }


def openapi(manifest: Manifest) -> dict[str, Any]:
    """An OpenAPI 3.1 document, so any OpenAPI-speaking tool can consume the site."""
    paths: dict[str, Any] = {}

    for action in servable(manifest):
        backend = action.backend
        if backend.kind != "http":
            continue
        path = urlsplit(backend.url).path or "/"
        method = backend.method.lower()

        operation: dict[str, Any] = {
            "operationId": action.name,
            "summary": action.description,
            "description": f"{action.description}\n\n{TIER_NOTE[action.tier]}",
            "tags": [action.tier],
            "responses": {
                "200": {
                    "description": "Extracted records",
                    "content": {"application/json": {"schema": _record_schema(action)}},
                }
            },
        }
        if method == "get":
            operation["parameters"] = _query_params(action)
        else:
            operation["requestBody"] = {
                "required": bool(action.input.required),
                "content": {
                    "application/x-www-form-urlencoded": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                name: {"type": prop.type}
                                for name, prop in action.input.properties.items()
                            },
                            "required": action.input.required,
                        }
                    }
                },
            }

        paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{manifest.host} (agent interface)",
            "version": "1.0.0",
            "description": (
                f"Compiled from {manifest.site} by shim on {manifest.compiled_at}. "
                f"{manifest.coverage.actions_shipped} verified action(s)."
            ),
        },
        "servers": [{"url": manifest.site}],
        "paths": paths,
    }


def llms_txt(manifest: Manifest) -> str:
    """The llms.txt convention: a short markdown briefing for a model."""
    actions = servable(manifest)
    lines = [
        f"# {manifest.host}",
        "",
        f"> Agent interface for {manifest.site}, compiled by shim. "
        f"{len(actions)} verified action(s). Every action below was executed "
        f"once against the live site before being published.",
        "",
    ]

    if manifest.site_description:
        lines += ["## About this site", "", manifest.site_description, ""]
    if actions:
        lines += ["## Actions", ""]
        for action in actions:
            args = ", ".join(
                f"{name}{'' if name in action.input.required else '?'}"
                for name in action.input.properties
            )
            lines.append(f"- **{action.name}({args})** — {action.description}")
            lines.append(f"  - risk: `{action.tier}` — {TIER_NOTE[action.tier]}")
            if action.check and action.check.items_found:
                lines.append(
                    f"  - verified: returned {action.check.items_found} record(s)"
                )
            lines.append(f"  - returns: {', '.join(action.extract.fields) or 'text'}")
        lines.append("")

    for proc in manifest.procedures:
        lines += [f"## How to: {proc.intent}", ""]
        for i, step in enumerate(proc.steps, 1):
            if step.action:
                args = ", ".join(f"{k}={v}" for k, v in step.with_.items())
                lines.append(f"{i}. call `{step.action}({args})`")
            elif step.url:
                lines.append(f"{i}. read {step.url}")
            else:
                lines.append(f"{i}. {step.note}")
        lines.append("")

    records = [(pg, r) for pg in manifest.pages for r in pg.records]
    if records:
        lines += ["## Content (declared by the site as JSON-LD)", ""]
        by_type: dict[str, int] = {}
        for _, r in records:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        lines.append("- " + ", ".join(f"{n} {t}" for t, n in sorted(by_type.items())))
        for pg, r in records[:8]:
            label = r.get("name") or r.get("headline") or r["type"]
            extras = [f"{k}={r[k]}" for k in ("price", "priceCurrency", "availability") if k in r]
            lines.append(f"- {r['type']}: {label}" + (f" ({', '.join(extras)})" if extras else "") + f" — {pg.url}")
        lines.append("")

    if manifest.exploration_summary:
        lines.extend(["", "## Exploration findings (model-observed, not verification)", manifest.exploration_summary])
    if manifest.notes:
        lines.extend(["", "## Browser guidance and limitations", *manifest.notes])
    readable = pages_markdown(manifest)
    if readable:
        lines += ["## Readable pages", "", readable, ""]

    skipped = manifest.coverage.skipped
    if skipped:
        lines += ["## Not available", ""]
        for skip in skipped[:12]:
            lines.append(f"- {skip.reason}: {skip.detail or skip.what}")
        lines.append("")

    lines += [
        "## How to call",
        "",
        f"- MCP (stdio): `shim serve {manifest.host}`",
        f"- OpenAPI: `./openapi.json`",
        f"- HTTP directly: see `./calls.sh`",
        "",
    ]
    return "\n".join(lines)


def pages_markdown(manifest: Manifest) -> str:
    return "\n\n---\n\n".join(
        f"## [{page.title or page.url}]({page.url})\n\n{page.markdown}"
        for page in manifest.pages if page.markdown
    )


def webmcp_js(manifest: Manifest) -> str:
    """A WebMCP registration script.

    Follows the shape of the WebMCP proposal (`navigator.modelContext`), which
    lets a page declare its own tools to an in-browser agent. The proposal is
    still moving, so this guards on feature detection and is inert elsewhere.
    """
    tools = []
    for action in servable(manifest):
        backend = action.backend
        if backend.kind != "http":
            continue
        tools.append({
            "name": action.name,
            "description": action.description,
            "inputSchema": action.input.model_dump(exclude_none=True),
            "annotations": {
                "readOnlyHint": action.tier == "read",
                "destructiveHint": action.tier == "irreversible",
            },
            "_endpoint": {"method": backend.method, "url": backend.url,
                          "fields": {k: v.model_dump(by_alias=True)
                                     for k, v in backend.fields.items()}},
        })

    return f"""// WebMCP registration for {manifest.host}, generated by shim.
// Serve this from the site itself so an in-browser agent discovers its tools.
(function () {{
  const TOOLS = {json.dumps(tools, indent=2)};

  if (!("modelContext" in navigator)) return; // not a WebMCP-capable agent

  for (const tool of TOOLS) {{
    navigator.modelContext.registerTool({{
      name: tool.name,
      description: tool.description,
      inputSchema: tool.inputSchema,
      annotations: tool.annotations,
      async execute(args) {{
        const spec = tool._endpoint;
        const params = new URLSearchParams();
        for (const [wire, source] of Object.entries(spec.fields)) {{
          if (source.from === "const") params.set(wire, source.value);
          else if (source.from === "input" && args[source.name] != null)
            params.set(wire, String(args[source.name]));
        }}
        const url = new URL(spec.url);
        if (spec.method === "GET") {{
          url.search = params.toString();
          const res = await fetch(url, {{ credentials: "same-origin" }});
          return {{ content: [{{ type: "text", text: await res.text() }}] }};
        }}
        const res = await fetch(url, {{
          method: "POST", credentials: "same-origin", body: params,
        }});
        return {{ content: [{{ type: "text", text: await res.text() }}] }};
      }},
    }});
  }}
}})();
"""


def calls_sh(manifest: Manifest) -> str:
    """Copy-pasteable shell calls: the lowest common denominator front door."""
    lines = [
        "#!/usr/bin/env bash",
        f"# Direct HTTP calls for {manifest.host}, generated by shim.",
        "# No server, no MCP client, no dependencies.",
        "",
    ]
    for action in servable(manifest):
        backend = action.backend
        if backend.kind != "http":
            continue
        # Prefer the input that actually passed the compile-time check, so the
        # generated call is known to work rather than merely plausible.
        verified = dict(action.check.input) if action.check and action.check.input else {}

        pairs = []
        for wire, source in backend.fields.items():
            if source.from_ == "const":
                pairs.append(f"{wire}={source.value}")
            elif source.from_ == "input":
                example = action.input.properties.get(source.name)
                sample = verified.get(source.name)
                if sample is None:
                    sample = (example.default if example and example.default else None) or (
                        example.enum[0] if example and example.enum else "VALUE"
                    )
                pairs.append(f"{wire}={sample}")
        query = "&".join(pairs)
        lines += [
            f"# {action.name} [{action.tier}] — {action.description}",
        ]
        if action.tier == "irreversible":
            lines.append("# WARNING: cannot be undone. Review before running.")
        if backend.method == "GET":
            lines.append(f"curl -sG '{backend.url}' --data-urlencode '{query}'")
        else:
            lines.append(f"curl -s -X POST '{backend.url}' -d '{query}'")
        lines.append("")
    return "\n".join(lines)


def agent_page(manifest: Manifest) -> str:
    """The /.agent page: one document describing the site's agent interface."""
    actions = servable(manifest)
    cov = manifest.coverage

    rows = "\n".join(
        f"""    <tr>
      <td><code>{a.name}</code></td>
      <td><span class="t t-{a.tier}">{a.tier}</span></td>
      <td>{escape(a.description)}</td>
      <td><code>{', '.join(a.input.properties) or '—'}</code></td>
      <td><code>{', '.join(a.extract.fields) or 'text'}</code></td>
      <td>{(a.check.items_found if a.check and a.check.items_found else 0)}</td>
    </tr>"""
        for a in actions
    )

    skips = "\n".join(
        f"    <li><strong>{s.reason}</strong> — {escape(s.detail or s.what)}</li>"
        for s in cov.skipped[:20]
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{manifest.host} — agent interface</title>
<style>
 body{{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:1000px;
      margin:40px auto;padding:0 24px;color:#1b1d21}}
 h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#6b707a;margin:0 0 28px}}
 table{{border-collapse:collapse;width:100%;font-size:13.5px}}
 th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid #e6e8ec;vertical-align:top}}
 th{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#8b8f9a}}
 code{{font:12.5px ui-monospace,Menlo,monospace;background:#f4f5f7;padding:1px 5px;border-radius:4px}}
 .t{{font:10.5px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.07em;
     padding:2px 6px;border-radius:4px;border:1px solid currentColor}}
 .t-read{{color:#1a7f64}} .t-reversible{{color:#a06b12}} .t-irreversible{{color:#c0392b}}
 .stats{{display:flex;gap:28px;margin:24px 0;padding:14px 16px;background:#f7f8fa;border-radius:9px}}
 .stats div span{{display:block;font-size:11px;text-transform:uppercase;
                  letter-spacing:.08em;color:#8b8f9a}}
 .stats div strong{{font-size:20px}}
 ul{{color:#6b707a;font-size:13.5px}} h2{{font-size:13px;text-transform:uppercase;
   letter-spacing:.1em;color:#8b8f9a;margin-top:34px}}
</style>
<h1>{manifest.host}</h1>
<p class="sub">Agent interface compiled from <a href="{manifest.site}">{manifest.site}</a>
  on {manifest.compiled_at}. Every action listed was executed against the live site
  before publication.</p>

<div class="stats">
  <div><span>actions</span><strong>{cov.actions_shipped}</strong></div>
  <div><span>candidates</span><strong>{cov.actions_generated}</strong></div>
  <div><span>pages read</span><strong>{cov.pages_crawled}</strong></div>
  <div><span>skipped</span><strong>{len(cov.skipped)}</strong></div>
</div>

<h2>Actions</h2>
<table>
  <tr><th>Tool</th><th>Risk</th><th>Description</th><th>Inputs</th><th>Returns</th><th>Verified</th></tr>
{rows or '    <tr><td colspan="6">No actions shipped.</td></tr>'}
</table>

<h2>Formats</h2>
<ul>
  <li><code>manifest.json</code> — the full compiled contract</li>
  <li><code>openapi.json</code> — OpenAPI 3.1</li>
  <li><code>llms.txt</code> — model-readable briefing</li>
  <li><code>webmcp.js</code> — in-browser tool registration</li>
  <li><code>calls.sh</code> — direct HTTP calls</li>
  <li>MCP over stdio — <code>shim serve {manifest.host}</code></li>
</ul>

{'<h2>Readable pages</h2><pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + escape(pages_markdown(manifest)) + '</pre>' if pages_markdown(manifest) else ''}

{'<h2>Not available</h2><ul>' + skips + '</ul>' if skips else ''}
"""


def emit_all(manifest: Manifest, out_dir: Path) -> list[Path]:
    """Writes every format. Returns the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    files: dict[str, str] = {
        "openapi.json": json.dumps(openapi(manifest), indent=2),
        "llms.txt": llms_txt(manifest),
        "pages.md": pages_markdown(manifest),
        "misc.md": "# Browser guidance and limitations\n\nThese notes are not verified executable tools.\n\n" +
                   (manifest.exploration_summary + "\n\n" if manifest.exploration_summary else "") +
                   "\n\n".join(manifest.notes + [f"Not shipped: {s.what}: {s.detail or s.reason}" for s in manifest.coverage.skipped]),
        "webmcp.js": webmcp_js(manifest),
        "calls.sh": calls_sh(manifest),
        "index.html": agent_page(manifest),
        "manifest.json": manifest.to_json(),
    }

    for name, content in files.items():
        path = out_dir / name
        path.write_text(content)
        if name == "calls.sh":
            path.chmod(0o755)
        written.append(path)

    return written
