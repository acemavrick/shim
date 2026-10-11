"""Turns form descriptors into manifest actions.

This is the diffing-free shortcut and the highest-leverage file in the project.
The decision it has to make -- which fields are agent parameters and which are
constants to pin -- is the one request diffing used to answer. The rule:
a visible named control is a parameter; a hidden field is a constant.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..manifest import (
    Action,
    Extract,
    FromConst,
    FromInput,
    HttpBackend,
    InputSchema,
    ParamSource,
    PropertySchema,
    Session,
    Skip,
    Source,
    Tier,
)
from .forms import Control, FormDescriptor, SubmitButton

# Only applied to POST forms: HTTP semantics make GET safe by definition, so a
# search box on a bookshop must not be flagged because it contains "book".
DANGER = re.compile(
    r"\b(buy|order|pay|checkout|purchase|delete|remove|cancel|confirm"
    r"|subscribe|donate|book|reserve|schedule|submit\s+order)\b",
    re.I,
)
GENERIC_SUBMIT = {"submit", "go", "ok", "send", "continue", "next", "apply"}
FREE_TEXT_KINDS = {"text", "number"}


def slug(raw: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return re.sub(r"_+", "_", out)[:40]


def _dedupe_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _path_noun(action: str) -> str | None:
    segs = [s for s in urlsplit(action).path.split("/") if s]
    if not segs:
        return None
    last = re.sub(r"\.\w+$", "", segs[-1])  # strip .php/.html
    noun = slug(last)
    return noun or None


def _has_free_text(desc: FormDescriptor) -> bool:
    return any(c.kind in FREE_TEXT_KINDS for c in desc.controls)


def action_name(desc: FormDescriptor, submit: SubmitButton | None) -> str:
    tokens: list[str] = []

    if submit and submit.text:
        candidate = slug(submit.text)
        if candidate and candidate not in GENERIC_SUBMIT:
            tokens = candidate.split("_")

    if not tokens:
        verb = "search" if desc.method == "GET" and _has_free_text(desc) else "submit"
        noun = _path_noun(desc.action)
        tokens = [verb, noun] if noun else [verb]

    name = "_".join(_dedupe_tokens([t for t in tokens if t]))
    if not re.match(r"^[a-z]", name):
        name = f"do_{name}" if name else "submit_form"
    return name or "submit_form"


def action_tier(desc: FormDescriptor, submit: SubmitButton | None) -> Tier:
    if desc.method == "GET":
        return "read"
    haystack = " ".join(
        filter(None, [submit.text if submit else None, desc.heading, urlsplit(desc.action).path])
    )
    return "irreversible" if DANGER.search(haystack) else "reversible"


def _describe(desc: FormDescriptor, submit: SubmitButton | None, params: list[str]) -> str:
    host = urlsplit(desc.action).netloc

    # A button reading "Go!" or "Submit" tells an agent nothing; prefer the
    # form's own heading, and fall back to what the request actually does.
    label = submit.text.rstrip(".") if submit and submit.text else None
    if label and slug(label) in GENERIC_SUBMIT:
        label = None

    if label is None and desc.method == "GET" and _has_free_text(desc):
        lead = f"Search {host}"
    else:
        lead = f"{label or desc.heading or 'Submit the form'} on {host}"

    if params:
        return f"{lead}. Takes: {', '.join(params)}."
    return f"{lead}."


def _property_for(control: Control) -> PropertySchema:
    label = control.label or control.wire.replace("_", " ")

    if control.kind == "checkbox":
        return PropertySchema(type="boolean", description=label)

    if control.kind in ("select", "radio"):
        return PropertySchema(
            type="string",
            description=label,
            enum=control.options or None,
            default=control.value,
        )

    if control.kind == "number":
        return PropertySchema(
            type="number", description=label,
            minimum=control.minimum, maximum=control.maximum,
        )

    return PropertySchema(
        type="string", description=label,
        pattern=control.pattern, maxLength=control.max_length,
    )


def form_to_actions(desc: FormDescriptor) -> tuple[list[Action], list[Skip]]:
    """One form yields one action per branching submit button.

    A form with two submit buttons carrying name+value (``intent=add_to_cart``
    vs ``intent=buy_now``) hits different server branches. Modelling it 1:1
    would silently always take the default branch.
    """
    where = f"{desc.page}#form{desc.form_index}"

    if "multipart" in desc.enctype:
        return [], [Skip(what=where, reason="multipart-upload",
                         detail="file uploads are out of scope")]

    if not desc.controls and not desc.submits:
        return [], [Skip(what=where, reason="no-submittable-fields")]

    # Branching buttons each become their own action; everything else is one.
    branches: list[SubmitButton | None] = [s for s in desc.submits if s.wire and s.value]
    if not branches:
        branches = [desc.submits[0] if desc.submits else None]

    actions: list[Action] = []
    for submit in branches:
        properties: dict[str, PropertySchema] = {}
        required: list[str] = []
        fields: dict[str, ParamSource] = {}
        labels: list[str] = []

        for control in desc.controls:
            if control.kind == "hidden":
                # Pinned, not exposed. A hidden field is part of the request.
                fields[control.wire] = FromConst(value=control.value or "")
                continue

            prop_name = slug(control.wire) or control.wire
            properties[prop_name] = _property_for(control)
            labels.append(control.label or prop_name)
            if control.required:
                required.append(prop_name)
            fields[control.wire] = FromInput(
                name=prop_name,
                true_value=control.value or "on" if control.kind == "checkbox" else None,
            )

        # The button's own name=value pair is part of the request it triggers.
        if submit and submit.wire and submit.value:
            fields[submit.wire] = FromConst(value=submit.value)

        if not fields:
            continue

        actions.append(
            Action(
                name=action_name(desc, submit),
                description=_describe(desc, submit, labels),
                tier=action_tier(desc, submit),
                input=InputSchema(properties=properties, required=required),
                backend=HttpBackend(
                    method=desc.method,
                    url=desc.action,
                    encoding="query" if desc.method == "GET" else "form",
                    fields=fields,
                ),
                # Left empty on purpose: the response shape is unknown until we
                # fire the request, so `check` infers the extractor from reality.
                extract=Extract(kind="css", fields={}),
                session=Session(scope="per-connection", carry_cookies=desc.method == "POST"),
                source=Source(
                    page=desc.page, form_index=desc.form_index,
                    submit=submit.text if submit else None,
                ),
            )
        )

    return actions, []


def dedupe_actions(actions: list[Action]) -> tuple[list[Action], list[Skip]]:
    """Site-wide forms (a header search box) appear on every page.

    Without this, a 14-page crawl yields search, search_2 ... search_14.
    """
    seen: dict[tuple[str, str, str], Action] = {}
    kept: list[Action] = []
    skips: list[Skip] = []

    for action in actions:
        backend = action.backend
        assert backend.kind == "http"
        key = (backend.method, backend.url.split("#")[0], ",".join(sorted(backend.fields)))
        if key in seen:
            skips.append(
                Skip(
                    what=f"{action.source.page if action.source else action.name}"
                         f"#form{action.source.form_index if action.source else 0}",
                    reason="duplicate-form",
                    detail=f"same request as {seen[key].name}",
                )
            )
            continue
        seen[key] = action
        kept.append(action)

    # Disambiguate any surviving name collisions.
    counts: dict[str, int] = {}
    for action in kept:
        counts[action.name] = counts.get(action.name, 0) + 1
    used: dict[str, int] = {}
    for action in kept:
        if counts[action.name] > 1:
            used[action.name] = used.get(action.name, 0) + 1
            action.name = f"{action.name}_{used[action.name]}"

    return kept, skips
