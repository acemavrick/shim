"""The compiler: explore -> translate -> check -> manifest.

Models learn the site and propose names and extractors at compile time.
Without a key, declared markup and deterministic heuristics provide a fallback.
Every shipped request passes the same runtime verification gate.
"""

from __future__ import annotations

import json
import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from time import perf_counter
from urllib.parse import urlsplit

from .check import check_action, probe_terms, sample_args
from .errors import exception_detail as _exception_detail
from .explore.crawl import crawl, _safe_root_redirect
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
    manifest = Manifest.model_validate_json(path.read_text())
    if manifest.run_id:
        from .history import load_version
        return load_version(host, manifest.run_id)
    return manifest


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


def _independent(action):
    return (action.tier == "read" and action.backend.kind == "http"
            and action.backend.method == "GET" and not action.backend.refetch_from
            and not (action.session and (action.session.carry_cookies or action.session.scope != "none"))
            and not any(f.from_ in {"page", "generate"} for f in action.backend.fields.values()))


async def _checked_actions(actions, probes, *, allow_writes, describer, concurrency,
                           check=None):
    """Continuously refill independent checks, draining before stateful barriers.

    Work completes in any order; results retain candidate order for stable names.
    """
    check = check or (lambda action: check_action(action, probes, allow_writes=allow_writes,
                                                  describer=describer))
    slots = asyncio.Semaphore(concurrency)
    async def bounded(action):
        async with slots:
            return await check(action)
    async with asyncio.TaskGroup() as group:
        index = 0
        while index < len(actions):
            if not _independent(actions[index]):
                action = actions[index]
                index += 1
                name = action.name
                checked, skip = await check(action)
                yield name, checked, skip
                continue
            pending = []
            while index < len(actions) and _independent(actions[index]):
                action = actions[index]
                pending.append((action.name, group.create_task(bounded(action))))
                index += 1
            for name, task in pending:
                checked, skip = await task
                yield name, checked, skip


async def compile_site(url: str, max_pages: int = 12, allow_writes: bool = False,
                       include_pages: bool = False, use_browser: bool = False,
                       intent: str | None = None, agent: bool | None = None,
                       emit: Emit = None) -> Manifest:
    # Validate outside TaskGroup to preserve the public error types.
    from .config import config
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"not a URL: {url!r}")
    if agent is True and not config().has_model:
        raise ValueError("browser exploration requires an Anthropic API key or a configured Codex CLI provider")
    async with asyncio.TaskGroup() as group:
        return await _compile_site(url, max_pages, allow_writes, include_pages, use_browser,
                                   intent, agent, emit, _group=group)


async def _compile_site(
    url: str,
    max_pages: int = 12,
    allow_writes: bool = False,
    include_pages: bool = False,
    use_browser: bool = False,
    intent: str | None = None,
    agent: bool | None = None,
    emit: Emit = None,
    _group=None,
) -> Manifest:
    started = perf_counter()
    phase = None
    phase_starts = {}
    timings = {}
    active_stages = set()
    destination = emit

    async def finish_phase(stage):
        if stage in phase_starts:
            timings[stage] = round((perf_counter() - phase_starts.pop(stage)) * 1000)
            active_stages.discard(stage)
            await _emit(destination, {"type": "timing", "stage": stage,
                                      "elapsed_ms": timings[stage]})

    async def timed_emit(event):
        nonlocal phase
        kind = event.get("type")
        if kind == "stage":
            stage = event["stage"]
            if stage not in {"agent", "explore"}:
                if phase is not None:
                    await finish_phase(phase)
                phase = stage
            phase_starts[stage] = perf_counter()
            active_stages.add(stage)
        elif kind == "stage_end":
            await finish_phase(event["stage"])
        elif kind == "done":
            for stage in list(phase_starts):
                await finish_phase(stage)
            event = {**event, "elapsed_ms": round((perf_counter() - started) * 1000),
                     "timings_ms": timings}
        if kind in {"stage", "stage_end"}:
            event = {**event, "active_stages": sorted(active_stages)}
        await _emit(destination, event)

    emit = timed_emit
    from .config import config as _config
    cfg = _config()
    parts = urlsplit(url)
    host = parts.netloc
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"not a URL: {url!r}")
    if agent is True and not cfg.has_model:
        raise ValueError("browser exploration requires an Anthropic API key or a configured Codex CLI provider")
    agent = cfg.has_model and not use_browser if agent is None else agent
    if agent and not intent:
        intent = "Learn this site's representative capabilities and compile a reusable agent-readable interface."
    from .history import new_run, publish_run
    run_id, run_dir = new_run(host)
    await _emit(emit, {"type": "run", "host": host, "run_id": run_id})

    coverage = Coverage()
    await _emit(emit, {"type": "stage", "stage": "explore", "url": url})

    from .translate.describe import make_completer, ModelDescriber
    from .translate.from_links import collect_links, link_actions
    page_html = {}
    early_describer = (ModelDescriber(make_completer(cfg, on_event=emit), page_html=page_html,
                                      intent=intent) if cfg.has_model else None)
    slots = asyncio.Semaphore(cfg.check_concurrency)
    candidates = {}
    early_checks = {}
    early_definitions = {}
    form_signatures = set()
    early_barrier = allow_writes
    seen_links = set()
    discovery_host = host

    def key(action):
        backend = action.backend
        if backend.kind != "http":
            return (backend.kind, backend.model_dump_json(), ())
        return (backend.method, backend.url.split("#")[0], tuple(sorted(backend.fields)))

    def payload(action, candidate_id):
        return {"candidate_id": candidate_id, "name": action.name,
                "description": action.description, "tier": action.tier,
                "inputs": list(action.input.properties),
                "schema": action.input.model_dump(exclude_none=True),
                "required": action.input.required,
                "definition": action.model_dump(by_alias=True, exclude_none=True)}

    async def announce(action):
        request_key = key(action)
        if request_key not in candidates:
            candidates[request_key] = f"tool-{len(candidates) + 1}"
            await _emit(emit, {"type": "tool_candidate", **payload(action, candidates[request_key])})
        return candidates[request_key]

    async def verify(action, probes, describer):
        candidate_id = await announce(action)
        async with slots:
            await _emit(emit, {"type": "tool_checking", **payload(action, candidate_id)})
            checked, skip = await check_action(action, probes, allow_writes=allow_writes,
                                                describer=describer)
            if skip is None:
                await _emit(emit, {"type": "action", **payload(checked, candidate_id),
                                  "items_found": checked.check.items_found if checked.check else None})
            else:
                await _emit(emit, {"type": "action_failed", **payload(checked, candidate_id),
                                  "error": checked.check.error if checked.check else skip.detail})
            return checked, skip

    def definition(action):
        # A request-shaped match alone cannot transfer a verification to a new
        # extractor, schema, session policy, or observed source.
        return action.model_dump(by_alias=True, exclude={"name", "description", "check"})

    def start_check(action, probes):
        request_key = key(action)
        if not early_barrier and _independent(action) and request_key not in early_checks:
            early_definitions[request_key] = definition(action)
            early_checks[request_key] = (list(probes), _group.create_task(
                verify(action.model_copy(deep=True), probes, early_describer)))

    async def discover_links(page):
        nonlocal discovery_host
        page_parts = urlsplit(page.url)
        # Preserve the user's library key, while following the root's www alias
        # for discovery. Unrelated origins never widen the candidate surface.
        if _safe_root_redirect(url, page.url):
            discovery_host = page_parts.netloc
        page_html[page.url] = page.html
        seen_links.update(collect_links([(page.url, page.html)], discovery_host))
        probes = probe_terms(list(page_html.values()))
        # Keep origin clusters separate: link_actions chooses the origin of its
        # first link, so mixing alias hosts could fabricate request templates.
        for origin_host in sorted({urlsplit(link).netloc for link in seen_links}):
            links = sorted(link for link in seen_links if urlsplit(link).netloc == origin_host)
            for action in link_actions(links):
                await announce(action)
                start_check(action, probes)

    async def on_page(page: Any, result: Any) -> None:
        nonlocal early_barrier
        coverage.pages_crawled += 1
        page_html[page.url] = page.html
        await _emit(emit, {"type": "page", "url": page.url, "title": page.title})
        probes = probe_terms(list(page_html.values()))
        for descriptor in getattr(result, "forms", []):
            if descriptor.signature in form_signatures:
                continue
            form_signatures.add(descriptor.signature)
            produced, _ = form_to_actions(descriptor)
            for action in produced:
                await announce(action)
                if not _independent(action):
                    early_barrier = True
                start_check(action, probes)
        await discover_links(page)

    async def explore_agent():
        from .explore.agent import AgentExplorer
        await _emit(emit, {"type": "stage", "stage": "agent", "intent": intent,
                           "step_budget": cfg.agent_steps})
        async def agent_event(event):
            nonlocal early_barrier
            if event.get("type") == "agent_tool":
                from .manifest import Action
                action = Action.model_validate(event["definition"])
                await announce(action)
                if not _independent(action):
                    early_barrier = True
                start_check(action, probe_terms(list(page_html.values())))
            elif event.get("type") == "agent_page":
                from .explore.crawl import CrawledPage
                page = CrawledPage(url=event["url"], html=event["html"], title=event.get("title"))
                await discover_links(page)
                await _emit(emit, {"type": "page", "url": page.url, "title": page.title,
                                   "source": "agent"})
            else:
                await _emit(emit, event)
        async def checkpoint(result):
            (run_dir / "raw" / "checkpoint.json").write_text(json.dumps({
                "recipe": result.recipe, "observed": [o.__dict__ for o in result.observed],
                "pages": [{"url": p.url, "title": p.title, "html": p.html} for p in result.pages],
                "model_trace": result.model_trace, "errors": result.errors,
                "site_description": result.site_description, "notes": result.notes}, indent=1))
        explorer = AgentExplorer(make_completer(cfg, on_event=emit, max_tokens=8192), intent,
                                 on_event=agent_event, step_budget=cfg.agent_steps,
                                 allow_writes=allow_writes, on_checkpoint=checkpoint)
        try:
            return await explorer.run(url)
        except Exception as exc:
            skip = Skip(what="agent", reason="fetch-error", detail=_exception_detail(exc))
            coverage.skipped.append(skip)
            await _emit(emit, {"type": "skip", **skip.model_dump()})
            return None
        finally:
            await _emit(emit, {"type": "stage_end", "stage": "agent"})

    agent_task = (_group.create_task(explore_agent())
                  if agent and intent and not allow_writes else None)

    if use_browser:
        from .explore.browser import browser_crawl
        crawled = await browser_crawl(url, max_pages=max_pages, on_page=on_page)
    else:
        crawled = await crawl(url, max_pages=max_pages, on_page=on_page)
    await _emit(emit, {"type": "stage_end", "stage": "explore"})
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

    await _emit(emit, {"type": "stage_end", "stage": "translate"})

    # The agent explorer: a model drives the site toward the intent, and what
    # it observes becomes candidates like everything else. Its pages join the
    # crawl so their forms, links and declared records are read too.
    agent_result = None
    if agent and intent and agent_task is None:
        agent_task = _group.create_task(explore_agent())
    if agent_task is not None:
        agent_result = await agent_task
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
            raw_dir = run_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "agent-trace.json").write_text(json.dumps({
                "intent": intent, "recipe": agent_result.recipe,
                "requests": [o.compact() for o in agent_result.observed if o.interesting],
                "summary": agent_result.summary, "model_trace": agent_result.model_trace,
                "errors": agent_result.errors}, indent=1))

    # Links with one varying segment are detail-page tools: /ebooks/1342 and
    # /ebooks/84 are one capability, get_ebook(id), not two pages.
    from .translate.from_links import collect_links, link_actions
    for page in crawled.pages:
        await discover_links(page)
    linked = []
    for origin_host in sorted({urlsplit(link).netloc for link in seen_links}):
        linked.extend(link_actions(sorted(link for link in seen_links
                                         if urlsplit(link).netloc == origin_host)))
    if linked:
        await _emit(emit, {"type": "stage", "stage": "links",
                           "patterns": [a.backend.url for a in linked if a.backend.kind == "http"]})
    actions.extend(linked)

    actions, dupe_skips = dedupe_actions(actions)
    coverage.skipped.extend(dupe_skips)
    coverage.actions_generated = len(actions)
    final_keys = {key(action) for action in actions}
    superseded = [task for request_key, (_, task) in early_checks.items()
                  if request_key not in final_keys]
    for task in superseded:
        task.cancel()
    if superseded:
        await asyncio.gather(*superseded, return_exceptions=True)
    for action in actions:
        await announce(action)

    # -------------------------------------------------------------- check
    await _emit(emit, {"type": "stage", "stage": "check", "candidates": len(actions)})

    probes = probe_terms([p.html for p in crawled.pages])

    describer = early_describer
    page_html.update({p.url: p.html for p in crawled.pages})
    if describer is not None:
        await _emit(emit, {"type": "stage", "stage": "describe", "model": cfg.model_name})

    async def final_check(action):
        if not _independent(action):
            # Early link discovery may precede a form that becomes a barrier.
            # Drain outer tasks too, not just this stage's ordered workers.
            await asyncio.gather(*(task for request_key, (_, task) in early_checks.items()
                                   if request_key in final_keys))
        cached = early_checks.get(key(action))
        if cached is not None:
            early_probes, task = cached
            checked, skip = await task
            if early_definitions[key(action)] != definition(action):
                # Richer agent evidence keeps precedence, but must pass its own
                # check. The provisional tool remains callable until this
                # replacement starts verification.
                return await verify(action, probes, describer)
            if skip is None:
                return checked, skip
            tried = {json.dumps(sample_args(action, term), sort_keys=True) for term in early_probes}
            remaining = [term for term in probes
                         if json.dumps(sample_args(action, term), sort_keys=True) not in tried]
            if not remaining:
                return checked, skip
            return await verify(action, remaining, describer)
        return await verify(action, probes, describer)

    shipped = []
    renamed: dict[str, str] = {}
    used_names: set[str] = set()
    async for original_name, checked, skip in _checked_actions(
            actions, probes, allow_writes=allow_writes, describer=describer,
            concurrency=cfg.check_concurrency, check=final_check):
        if skip:
            coverage.skipped.append(skip)
            await _emit(emit, {
                "type": "action_failed",
                "candidate_id": candidates[key(checked)],
                "name": checked.name,
                "tier": checked.tier,
                "error": checked.check.error if checked.check else None,
            })
            continue
        base_name = checked.name
        suffix = 1
        while checked.name in used_names:
            suffix += 1
            checked.name = f"{base_name}_{suffix}"
        used_names.add(checked.name)
        shipped.append(checked)
        renamed[original_name] = checked.name
        await _emit(emit, {
            "type": "action",
            "candidate_id": candidates[key(checked)],
            "name": checked.name,
            "description": checked.description,
            "tier": checked.tier,
            "inputs": list(checked.input.properties),
            "schema": checked.input.model_dump(exclude_none=True),
            "definition": checked.model_dump(by_alias=True, exclude_none=True),
            "required": checked.input.required,
            "items_found": checked.check.items_found if checked.check else None,
        })

    final_keys = {key(action) for action in actions}
    for request_key, candidate_id in candidates.items():
        if request_key not in final_keys:
            await _emit(emit, {"type": "action_failed", "candidate_id": candidate_id,
                              "error": "Candidate superseded by the final evidence."})
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
        run_id=run_id,
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

    path = await publish_run(manifest, run_dir)

    # Publish every front door, not just ours.
    emitted = list((run_dir / "agent").iterdir())

    await _emit(emit, {
        "type": "done",
        "host": host,
        "run_id": run_id,
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
