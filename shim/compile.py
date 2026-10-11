"""The compiler: explore -> translate -> check -> manifest.

Models learn the site and propose names and extractors at compile time.
Without a key, declared markup and deterministic heuristics provide a fallback.
Every shipped request passes the same runtime verification gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .check import check_action, probe_terms
from .explore.crawl import crawl
from .manifest import Coverage, Manifest, Page, Skip
from .translate.from_form import dedupe_actions, form_to_actions

SITES_DIR = Path("sites")
Emit = Callable[[dict[str, Any]], Any] | None


def manifest_path(host: str) -> Path:
    return SITES_DIR / host / "manifest.json"


def load_manifest(host: str) -> Manifest:
    path = manifest_path(host)
    if not path.exists():
        raise FileNotFoundError(
            f"no manifest for {host}. Run: shim compile <url>"
        )
    return Manifest.model_validate_json(path.read_text())


def save_manifest(manifest: Manifest) -> Path:
    path = manifest_path(manifest.host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.to_json())
    return path


async def _emit(emit: Emit, event: dict[str, Any]) -> None:
    if emit is None:
        return
    result = emit(event)
    if hasattr(result, "__await__"):
        await result


async def compile_site(
    url: str,
    max_pages: int = 12,
    allow_writes: bool = False,
    include_pages: bool = False,
    use_browser: bool = False,
    intent: str | None = None,
    agent: bool | None = None,
    emit: Emit = None,
) -> Manifest:
    from .config import config as _config
    cfg = _config()
    parts = urlsplit(url)
    host = parts.netloc
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"not a URL: {url!r}")
    if agent is True and not cfg.has_model:
        raise ValueError("browser exploration requires an Anthropic API key")
    agent = cfg.has_model and not use_browser if agent is None else agent
    if agent and not intent:
        intent = "Learn this site's representative capabilities and compile a reusable agent-readable interface."

    coverage = Coverage()
    await _emit(emit, {"type": "stage", "stage": "explore", "url": url})

    async def on_page(page: Any, _result: Any) -> None:
        coverage.pages_crawled += 1
        await _emit(emit, {"type": "page", "url": page.url, "title": page.title})

    if use_browser:
        from .explore.browser import browser_crawl
        crawled = await browser_crawl(url, max_pages=max_pages, on_page=on_page)
    else:
        crawled = await crawl(url, max_pages=max_pages, on_page=on_page)
    coverage.pages_crawled = len(crawled.pages)
    coverage.pages_failed = crawled.pages_failed
    coverage.forms_found = len(crawled.forms)
    coverage.skipped.extend(crawled.skips)

    for skip in crawled.skips:
        await _emit(emit, {"type": "skip", **skip.model_dump()})

    # ---------------------------------------------------------- translate
    await _emit(emit, {"type": "stage", "stage": "translate",
                       "forms": len(crawled.forms)})

    actions = []
    for descriptor in crawled.forms:
        produced, skips = form_to_actions(descriptor)
        actions.extend(produced)
        coverage.skipped.extend(skips)
        for skip in skips:
            await _emit(emit, {"type": "skip", **skip.model_dump()})

    # Endpoints the page's own JavaScript called are candidates too.
    observed = getattr(crawled, "endpoints", [])
    if observed:
        from .translate.from_endpoint import endpoint_to_action
        await _emit(emit, {"type": "stage", "stage": "observed",
                           "endpoints": len(observed)})
        for endpoint in observed:
            produced = endpoint_to_action(endpoint)
            if produced is not None:
                actions.append(produced)

    # The agent explorer: a model drives the site toward the intent, and what
    # it observes becomes candidates like everything else. Its pages join the
    # crawl so their forms, links and declared records are read too.
    agent_result = None
    if agent and intent:
        from .config import config as _cfg
        if _cfg().has_model:
            from .explore.agent import AgentExplorer
            from .translate.describe import AnthropicCompleter
            await _emit(emit, {"type": "stage", "stage": "agent", "intent": intent})
            explorer = AgentExplorer(AnthropicCompleter(_cfg(), on_event=emit, max_tokens=8192), intent, on_event=emit,
                                     allow_writes=allow_writes)
            try:
                agent_result = await explorer.run(url)
            except Exception as exc:
                coverage.skipped.append(Skip(what="agent", reason="fetch-error",
                                             detail=f"{type(exc).__name__}: {exc}"))
            if agent_result is not None:
                for error in agent_result.errors:
                    coverage.skipped.append(Skip(what="agent-write-down", reason="check-failed", detail=error))
                    await _emit(emit, {"type": "skip", "what": "agent-write-down", "reason": "check-failed", "detail": error})
                # Prefer rendered evidence over the initial SPA shell at the same URL.
                merged_pages = {p.url: p for p in crawled.pages}
                merged_pages.update({p.url: p for p in agent_result.pages})
                crawled.pages = list(merged_pages.values())
                coverage.pages_crawled = len(crawled.pages)
                await _emit(emit, {"type": "agent_done", "steps": agent_result.steps_taken,
                                   "observed": len([o for o in agent_result.observed if o.interesting]),
                                   "tools": [a.name for a in agent_result.tools],
                                   "summary": agent_result.summary})
                actions.extend(agent_result.tools)
                raw_dir = manifest_path(host).parent / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / "agent-trace.json").write_text(json.dumps({
                    "intent": intent, "recipe": agent_result.recipe,
                    "requests": [o.compact() for o in agent_result.observed if o.interesting],
                    "summary": agent_result.summary}, indent=1))

    # Links with one varying segment are detail-page tools: /ebooks/1342 and
    # /ebooks/84 are one capability, get_ebook(id), not two pages.
    from .translate.from_links import collect_links, link_actions
    linked = link_actions(collect_links([(p.url, p.html) for p in crawled.pages], host))
    if linked:
        await _emit(emit, {"type": "stage", "stage": "links",
                           "patterns": [a.backend.url for a in linked if a.backend.kind == "http"]})
    actions.extend(linked)

    actions, dupe_skips = dedupe_actions(actions)
    coverage.skipped.extend(dupe_skips)
    coverage.actions_generated = len(actions)

    # -------------------------------------------------------------- check
    await _emit(emit, {"type": "stage", "stage": "check", "candidates": len(actions)})

    probes = probe_terms([p.html for p in crawled.pages])

    describer = None
    if cfg.has_model:
        from .translate.describe import AnthropicCompleter, ModelDescriber
        describer = ModelDescriber(AnthropicCompleter(cfg, on_event=emit),
                                   page_html={p.url: p.html for p in crawled.pages},
                                   intent=intent)
        await _emit(emit, {"type": "stage", "stage": "describe", "model": cfg.model})

    shipped = []
    renamed: dict[str, str] = {}
    used_names: dict[str, int] = {}
    for action in actions:
        original_name = action.name
        checked, skip = await check_action(action, probes, allow_writes=allow_writes,
                                           describer=describer)
        if checked.name in used_names:
            used_names[checked.name] += 1
            checked.name = f"{checked.name}_{used_names[checked.name]}"
        else:
            used_names[checked.name] = 1
        if skip:
            coverage.skipped.append(skip)
            await _emit(emit, {
                "type": "action_failed",
                "name": checked.name,
                "tier": checked.tier,
                "error": checked.check.error if checked.check else None,
            })
            continue
        shipped.append(checked)
        renamed[original_name] = checked.name
        await _emit(emit, {
            "type": "action",
            "name": checked.name,
            "description": checked.description,
            "tier": checked.tier,
            "inputs": list(checked.input.properties),
            "schema": checked.input.model_dump(exclude_none=True),
            "definition": checked.model_dump(by_alias=True, exclude_none=True),
            "required": checked.input.required,
            "items_found": checked.check.items_found if checked.check else None,
        })

    coverage.actions_shipped = len(shipped)
    if describer is not None:
        await _emit(emit, {"type": "describe_done", "calls": describer.calls,
                           "failures": describer.failures})

    # With an intent and a model, write the procedure: how an agent gets it
    # done with these tools. Validated so it can only reference shipped ones.
    procedures = []
    if agent_result is not None and agent_result.procedure:
        from .manifest import Procedure, Step
        shipped_names = {a.name for a in shipped}
        steps = []
        for st in agent_result.procedure:
            if st.action and st.action in renamed:
                st = st.model_copy(update={"action": renamed[st.action]})
            if st.action and st.action not in shipped_names:
                steps.append(Step(note=f"(unverified) call {st.action} with {st.with_}"))
            else:
                steps.append(st)
        procedures.append(Procedure(intent=intent or "", steps=steps))
        await _emit(emit, {"type": "procedure", "intent": intent, "steps": len(steps)})
    elif intent and describer is not None and shipped:
        from .manifest import Procedure
        from .translate.describe import propose_procedure
        steps = await propose_procedure(describer._complete, intent, shipped,
                                        [(p.url, p.title) for p in crawled.pages])
        if steps:
            procedures.append(Procedure(intent=intent, steps=steps))
            await _emit(emit, {"type": "procedure", "intent": intent, "steps": len(steps)})

    # Declared structured data rides along on every compile: it costs one
    # parse per page and is the only content that needs no inference at all.
    from .translate.jsonld import jsonld_records, record_key
    from .translate.content import page_markdown
    pages = []
    seen_records: set[tuple[str, str]] = set()
    for crawled_page in crawled.pages:
        records = []
        for record in jsonld_records(crawled_page.html):
            key = record_key(record)
            if key[1] and key in seen_records:
                continue  # the site-wide Organization block, repeated per page
            seen_records.add(key)
            records.append(record)
        coverage.records_extracted += len(records)
        markdown = page_markdown(crawled_page.html, crawled_page.url)
        if records or markdown or include_pages:
            pages.append(Page(url=crawled_page.url, title=crawled_page.title, records=records,
                              markdown=markdown or None))
    if coverage.records_extracted:
        await _emit(emit, {
            "type": "records",
            "count": coverage.records_extracted,
            "types": sorted({r["type"] for pg in pages for r in pg.records}),
        })

    manifest = Manifest(
        site=f"{urlsplit(url).scheme}://{host}",
        host=host,
        compiled_at=datetime.now(timezone.utc).isoformat(),
        intent=intent,
        notes=agent_result.notes if agent_result is not None else [],
        exploration_summary=agent_result.summary if agent_result is not None else None,
        site_description=agent_result.site_description if agent_result is not None else None,
        procedures=procedures,
        pages=pages,
        actions=shipped,
        coverage=coverage,
    )

    path = save_manifest(manifest)

    # Publish every front door, not just ours.
    from .emit import emit_all
    emitted = emit_all(manifest, path.parent / "agent")

    await _emit(emit, {
        "type": "done",
        "host": host,
        "path": str(path),
        "shipped": coverage.actions_shipped,
        "generated": coverage.actions_generated,
        "records": coverage.records_extracted,
        "notes": manifest.notes,
        "exploration_summary": manifest.exploration_summary,
        "site_description": manifest.site_description,
        "procedures": [{"intent": pr.intent,
                        "steps": [st.model_dump(by_alias=True, exclude_none=True) for st in pr.steps]}
                       for pr in procedures],
        "pages": coverage.pages_crawled,
        "skipped": [s.model_dump() for s in coverage.skipped],
        "blocked": crawled.blocked,
        "formats": [f.name for f in emitted],
    })
    return manifest
