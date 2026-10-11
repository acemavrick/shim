"""Stage 3: check.

Fires each action once and keeps only the ones that actually worked.

This is load-bearing, not decorative. Form-first extraction reads the declared
method and action out of the markup -- but if a page intercepts submission in
JavaScript, those declarations are fiction and the compiler will emit a
confident, plausible, broken template. Firing the request is the only way to
find out. No ``passed``, no tool.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .manifest import Action, CheckResult, Extract, Skip
from .runtime.extract import run_extract
from .runtime.http import send_http
from .translate.infer import (
    detail_extract,
    infer_by_diff,
    infer_json,
    infer_listing,
    prune_empty_fields,
)

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "have", "more",
    "about", "they", "will", "would", "there", "their", "what", "when", "which",
    "page", "home", "http", "https", "html", "search", "menu", "click", "here",
}
WORD = re.compile(r"[a-z]{4,}")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}


def probe_terms(page_htmls: list[str], limit: int = 4) -> list[str]:
    """Builds probe words from the site's own records.

    Two constraints pull against each other. A probe must match something --
    a word from page prose like "class" finds no packages on PyPI. But probes
    must also *discriminate*: broad strings like "a" and "e" substring-match
    nearly every row, so two queries return the same results and a working
    search looks indistinguishable from a navigation sidebar.

    Words taken from the repeating records on the page satisfy both: they are
    by construction present in the index, and words drawn from *different*
    records return disjoint result sets.
    """
    buckets: list[list[str]] = []

    for html in page_htmls:
        spec = infer_listing(html)
        if not spec.items:
            continue
        records = run_extract(html, spec).records
        for record in records:
            text = " ".join(str(v) for v in record.values() if v)
            words = [
                w for w in WORD.findall(text.lower())
                if w not in STOPWORDS and len(w) >= 5
            ]
            if words:
                buckets.append(words)

    # Sample from widely separated records so the terms do not co-occur.
    terms: list[str] = []
    if buckets:
        step = max(1, len(buckets) // limit)
        for i in range(0, len(buckets), step):
            for word in buckets[i]:
                if word not in terms:
                    terms.append(word)
                    break
            if len(terms) >= limit:
                break

    if len(terms) < 2:
        # No records to learn from: fall back to page prose, then to broad
        # strings, accepting that broad probes may not discriminate.
        counts: Counter[str] = Counter()
        for html in page_htmls:
            for word in WORD.findall(html.lower()):
                if word not in STOPWORDS:
                    counts[word] += 1
        terms += [w for w, _ in counts.most_common(limit) if w not in terms]
        for fallback in ("a", "e"):
            if fallback not in terms:
                terms.append(fallback)

    return terms


def term_from_records(records: list[dict[str, Any]], avoid: set[str]) -> str | None:
    """Picks a discriminating probe word from a page's own records.

    A results page is, by construction, the best source of words the search
    index contains -- better than anything the crawl found on help or about
    pages. The *last* record is used so the word is unlikely to co-occur with
    whatever matched the first probe.
    """
    for record in reversed(records):
        text = " ".join(str(v) for v in record.values() if v)
        for word in WORD.findall(text.lower()):
            if len(word) >= 5 and word not in STOPWORDS and word not in avoid:
                return word
    return None


def is_local(host: str) -> bool:
    return host.split(":")[0] in LOCAL_HOSTS


def sample_args(action: Action, probe: str, variant: int = 0) -> dict[str, Any]:
    """Minimal plausible input: required fields, plus the first free-text field.

    ``variant`` picks among observed example values (link ids), so the control
    probe of a detail-page tool varies the id rather than a search word.
    """
    args: dict[str, Any] = {}
    filled_text = False

    for name, prop in action.input.properties.items():
        needed = name in action.input.required
        if prop.examples:
            value = prop.examples[min(variant, len(prop.examples) - 1)]
            args[name] = int(value) if prop.type == "integer" else float(value) if prop.type == "number" else value
            continue
        if prop.enum:
            if needed or prop.default is None:
                args[name] = prop.default or prop.enum[0]
            continue
        if prop.type == "boolean":
            continue  # a browser omits an unchecked box
        if prop.type in ("number", "integer"):
            if needed:
                args[name] = prop.minimum if prop.minimum is not None else 1
            continue
        if needed or not filled_text:
            args[name] = probe
            filled_text = True

    return args


def has_free_text(action: Action) -> bool:
    """True if some input is open text, i.e. something we can vary."""
    return any(
        prop.type == "string" and not prop.enum
        for prop in action.input.properties.values()
    )


def _fingerprint(records: list[dict[str, Any]]) -> str:
    """Order-sensitive identity of an extraction, for comparing two probes."""
    return json.dumps(records, sort_keys=True, ensure_ascii=False, default=str)


async def _probe_once(
    action: Action, args: dict[str, Any]
) -> tuple[Any, list[dict[str, Any]]] | None:
    """Fires the action and extracts with its current spec. None on transport failure."""
    try:
        res = await send_http(action.backend, args)
    except Exception:
        return None
    extracted = run_extract(res.body, action.extract, res.url)
    non_empty = [
        r for r in extracted.records
        if any(v not in (None, "", []) for v in r.values())
    ]
    return res, non_empty


async def check_action(
    action: Action,
    probes: list[str],
    allow_writes: bool = False,
    describer: Any = None,
) -> tuple[Action, Skip | None]:
    """Probes the action, infers its extractor from reality, verifies it.

    For anything with a free-text input the extractor is chosen by **diffing
    two responses to different queries**: whatever changed is the answer, and
    whatever stayed identical is navigation. Non-emptiness alone is not a test
    -- an extractor aimed at a sidebar returns a confident, plausible, constant
    answer for every query, which is worse than returning nothing.
    """
    assert action.backend.kind == "http"
    host = action.backend.url.split("/")[2] if "//" in action.backend.url else ""

    # Never fire a write at a site we do not own.
    if action.tier != "read" and not (allow_writes or is_local(host)):
        action.check = CheckResult(
            passed=False,
            error="write action on a third-party site; not verified, so not shipped",
        )
        return action, Skip(
            what=action.name, reason="check-failed",
            detail="write action on a third-party site",
        )

    terms = probes or ["a"]
    varies = has_free_text(action)
    last_error: str | None = None
    control_term = ""

    for probe in terms:
        args = sample_args(action, probe)
        try:
            primary = await send_http(action.backend, args)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        if primary.blocked:
            action.check = CheckResult(
                input=args, passed=False, status=primary.status, error="bot protection"
            )
            return action, Skip(
                what=action.name, reason="bot-protection",
                detail=f"HTTP {primary.status}",
            )

        if primary.status >= 400:
            last_error = f"HTTP {primary.status}"
            continue

        control_body = None
        control_args = {}
        if varies and action.tier == "read":
            control_term = next((t for t in terms if t != probe), f"{probe}qx")
            control_args = sample_args(action, control_term, variant=1)
            if control_args != args:
                try:
                    control = await send_http(action.backend, control_args)
                except Exception as exc:
                    last_error = f"control probe failed: {type(exc).__name__}: {exc}"
                    continue
                if control.blocked or control.status >= 400:
                    last_error = f"control probe failed: HTTP {control.status}"
                    continue
                control_body = control.body

        # Model output is the primary translation path. Heuristics are used
        # only when no model is configured, not to rescue invalid proposals.
        if describer is not None:
            proposal = await describer(action, primary.body, control_body, args, control_args)
            if proposal is None:
                last_error = "model proposal failed; candidate not verified"
                break
            if not proposal.useful:
                action.check = CheckResult(input=args, passed=False,
                                           error=f"not useful: {proposal.reason or 'model-filtered'}")
                return action, Skip(what=action.name, reason="not-useful", detail=proposal.reason)
            from .translate.describe import apply_proposal
            apply_proposal(action, proposal)
            if proposal.extract is not None:
                action.extract = proposal.extract
        elif "json" in primary.content_type:
            try:
                action.extract = infer_json(json.loads(primary.body))
            except ValueError:
                action.extract = infer_listing(primary.body)
        elif varies:
            # Second query, so the extractor can be chosen by what moved. Mine
            # the control word from the first response's own records: the
            # crawl's probe words come from prose and may match nothing, but a
            # results page always contains words its index knows.
            # Crawl-derived terms come from different records and so return
            # different rows; words mined from this response's own results
            # tend to re-match the same rows, so they are only a fallback.
            if control_body is None and action.tier == "read":
                provisional = run_extract(primary.body, infer_listing(primary.body), primary.url)
                control_term = term_from_records(provisional.records, avoid={probe}) or f"{probe}qx"
                try:
                    control = await send_http(action.backend, sample_args(action, control_term, variant=1))
                except Exception as exc:
                    last_error = f"control probe failed: {type(exc).__name__}: {exc}"
                    continue
                control_body = control.body if control.status < 400 and not control.blocked else None

            # Declared records beat any inferred selector.
            from .translate.jsonld import jsonld_records
            is_detail = action.backend.kind == "http" and "{" in action.backend.url
            if jsonld_records(primary.body):
                action.extract = Extract(kind="jsonld", fields={})
            elif is_detail:
                # A page per id is one record, not a list of them.
                action.extract = detail_extract(primary.body)
            else:
                diffed = infer_by_diff(primary.body, control_body) if control_body else None
                action.extract = diffed or infer_listing(primary.body)
            if not is_detail and action.extract.kind == "css" and not action.extract.items:
                last_error = "nothing in the response changed between two queries"
        else:
            action.extract = infer_listing(primary.body)
            control_body = None

        extracted = run_extract(primary.body, action.extract, primary.url)

        # Drop fields the real response does not fill, then re-extract so the
        # recorded count reflects the shipped spec.
        pruned = prune_empty_fields(action.extract, extracted.records) if action.extract.kind == "css" else action.extract
        if pruned.fields != action.extract.fields:
            action.extract = pruned
            extracted = run_extract(primary.body, action.extract, primary.url)

        non_empty = [
            r for r in extracted.records
            if any(v not in (None, "", []) for v in r.values())
        ]
        if not non_empty and action.tier != "read":
            # A write answers with a confirmation, not a listing. 2xx is the
            # verification; keep whatever the response said as its record.
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", primary.body)).strip()[:200]
            action.extract = Extract(kind="css", fields={"text": "body"})
            action.check = CheckResult(
                input=args, passed=True, items_found=1, status=primary.status,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
            return action, None
        if not non_empty:
            last_error = "no records extracted from the response"
            continue

        # Final gate: the shipped spec must actually respond to input. Writes
        # are exempt -- "Product added." is the same for every product.
        if varies and control_body is not None and action.tier == "read":
            control_records = run_extract(control_body, action.extract, primary.url).records
            if _fingerprint(control_records) == _fingerprint(extracted.records):
                last_error = (
                    "extractor returns identical records for two different "
                    "queries; it is reading page furniture, not results"
                )
                action.check = CheckResult(
                    input=args, passed=False, status=primary.status, error=last_error
                )
                return action, Skip(
                    what=action.name, reason="check-failed", detail=last_error
                )

        action.check = CheckResult(
            input=args, passed=True, items_found=len(non_empty),
            status=primary.status, checked_at=datetime.now(timezone.utc).isoformat(),
        )
        return action, None

    action.check = CheckResult(
        passed=False, error=last_error or "no probe produced a usable response"
    )
    return action, Skip(what=action.name, reason="check-failed", detail=last_error)
