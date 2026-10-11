"""Turns an observed JSON endpoint into a manifest action.

A captured request is in some ways better evidence than a form: the parameter
names are the server's own, and the response sample shows the exact shape to
extract. What it does not tell you is which parameters are *meaningful* versus
incidental, which is the judgement made below.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from ..explore.browser import OPAQUE_VALUE_LENGTH, ObservedEndpoint
from ..manifest import (
    Action,
    Extract,
    FromConst,
    FromInput,
    HttpBackend,
    InputSchema,
    ParamSource,
    PropertySchema,
    Source,
)
from .infer import infer_json

QUERYISH = {"q", "query", "search", "term", "keyword", "keywords", "text"}
NOISE_SEGMENTS = {"api", "v1", "v2", "v3", "rest", "json", "graphql", "_next", "data"}


def _slug(raw: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_"))[:40]


def _name_for(endpoint: ObservedEndpoint) -> str:
    segments = [s for s in urlsplit(endpoint.url).path.split("/") if s]
    meaningful = [
        _slug(re.sub(r"\.\w+$", "", s))
        for s in segments
        if _slug(s) and _slug(s) not in NOISE_SEGMENTS and not s.isdigit()
    ]
    noun = meaningful[-1] if meaningful else "endpoint"
    verb = "search" if any(p in QUERYISH for p in endpoint.params) else "get"
    return f"{verb}_{noun}" if noun != verb else verb


def _is_opaque(value: str) -> bool:
    """Session tokens and cache-busters are not parameters to expose."""
    return len(value) >= OPAQUE_VALUE_LENGTH and " " not in value


def endpoint_to_action(endpoint: ObservedEndpoint) -> Action | None:
    if not endpoint.sample:
        return None

    try:
        body = json.loads(endpoint.sample)
    except ValueError:
        return None

    extract = infer_json(body)
    if not extract.fields:
        return None  # nothing worth returning

    parts = urlsplit(endpoint.url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    fields: dict[str, ParamSource] = {}
    properties: dict[str, PropertySchema] = {}

    for name, value in endpoint.params.items():
        if _is_opaque(value):
            # Pin it: required by the server, meaningless to an agent.
            fields[name] = FromConst(value=value)
            continue
        key = _slug(name) or name
        properties[key] = PropertySchema(
            type="string",
            description=f"{name} parameter (observed value: {value!r})",
            default=value,
        )
        fields[name] = FromInput(name=key)

    if not properties:
        return None  # a fixed URL is a page, not a callable action

    return Action(
        name=_name_for(endpoint),
        description=(
            f"Call the {parts.netloc} JSON endpoint {parts.path}. "
            f"Observed while the page loaded. Takes: {', '.join(properties)}."
        ),
        tier="read",
        input=InputSchema(properties=properties, required=[]),
        backend=HttpBackend(
            method="GET", url=base, encoding="query", fields=fields,
            headers={"accept": "application/json"},
        ),
        extract=extract,
        source=Source(page=endpoint.url, form_index=-1, submit="observed-xhr"),
    )
