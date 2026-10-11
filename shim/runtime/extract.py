"""Turns a response into JSON records.

Shared by ``check`` and ``serve`` -- the whole point of the split. If these two
ever extracted differently, a check could pass while the live tool call returned
garbage, so there is exactly one implementation and both import it.
"""

from __future__ import annotations

import json
import re
import warnings

# soupsieve warns about selector syntax it still accepts; a stored spec must
# never print at call time (stdio MCP keeps stdout for the protocol).
warnings.filterwarnings("ignore", category=FutureWarning, module="soupsieve")
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
import soupsieve

from ..manifest import Extract

URL_ATTRS = {"href", "src", "action", "poster", "data-href"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")


@dataclass
class ExtractResult:
    records: list[dict[str, Any]]
    items_found: int


@dataclass(frozen=True)
class FieldSpec:
    selector: str
    attr: str | None = None
    cast: str | None = None


@lru_cache(maxsize=512)
def parse_field_spec(spec: str) -> FieldSpec:
    s = spec
    cast = None
    bar = s.rfind("|")
    if bar != -1 and s[bar + 1 :].strip() == "number":
        cast = "number"
        s = s[:bar]

    attr = None
    at = s.rfind("@")
    if at != -1:
        attr = s[at + 1 :].strip()
        s = s[:at]

    return FieldSpec(selector=s.strip(), attr=attr, cast=cast)


def to_number(raw: str) -> float | None:
    """Pulls the first number out of text, tolerating currency and separators."""
    m = _NUMBER.search(raw.replace(",", "").replace(" ", ""))
    if not m:
        return None
    value = float(m.group(0))
    return int(value) if value.is_integer() else value


def _finish(raw: str | None, f: FieldSpec, base_url: str | None) -> Any:
    if raw is None:
        return None
    text = _WS.sub(" ", raw).strip()
    if f.cast == "number":
        return to_number(text)
    if f.attr in URL_ATTRS and base_url:
        try:
            return urljoin(base_url, text)
        except ValueError:
            return text
    return text


def _read(node: Tag, f: FieldSpec) -> str | None:
    if f.attr:
        value = node.get(f.attr)
        if isinstance(value, list):  # bs4 returns lists for e.g. class
            return " ".join(value)
        return value
    return node.get_text()


@lru_cache(maxsize=512)
def _css_selector(selector: str) -> soupsieve.SoupSieve | None:
    # Compile once for every response/record using this selector. Invalid stored
    # selectors keep their existing missing-value behavior.
    try:
        return soupsieve.compile(selector)
    except Exception:
        return None


def extract_from_html(html: str, spec: Extract, base_url: str | None = None) -> ExtractResult:
    soup = BeautifulSoup(html, "lxml")
    specs = {k: parse_field_spec(v) for k, v in spec.fields.items()}
    selectors = {f.selector: _css_selector(f.selector) for f in specs.values() if f.selector}

    # No `items` selector means the whole document is one record.
    try:
        roots: list[Tag | None] = list(soup.select(spec.items)) if spec.items else [None]
    except Exception:
        roots = []

    records: list[dict[str, Any]] = []
    for root in roots:
        rec: dict[str, Any] = {}
        scope = root if root is not None else soup
        nodes: dict[str, Tag | None] = {}
        for key, f in specs.items():
            if f.selector in nodes:
                node = nodes[f.selector]
            else:
                if f.selector == "":
                    node = scope if isinstance(scope, Tag) else None
                elif selectors[f.selector] is None:
                    node = None
                else:
                    try:
                        node = selectors[f.selector].select_one(scope)
                    except Exception:
                        node = None
                nodes[f.selector] = node
            rec[key] = _finish(_read(node, f), f, base_url) if node is not None else None
        records.append(rec)

    return ExtractResult(records=records, items_found=len(records))


@lru_cache(maxsize=512)
def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(path.split(".")) if path else ()


def _at_segments(root: Any, segments: tuple[str, ...]) -> Any:
    if not segments:
        return root
    cur = root
    for seg in segments:
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _at_path(root: Any, path: str) -> Any:
    return _at_segments(root, _path_segments(path))


def extract_from_json(body: Any, spec: Extract) -> ExtractResult:
    target = _at_path(body, spec.items) if spec.items else body
    items = target if isinstance(target, list) else [target]
    specs = []
    for key, raw in spec.fields.items():
        field = parse_field_spec(raw)
        specs.append((key, _path_segments(field.selector), field.cast))

    records: list[dict[str, Any]] = []
    for item in items:
        rec: dict[str, Any] = {}
        for key, segments, cast in specs:
            value = _at_segments(item, segments)
            if cast == "number" and isinstance(value, str):
                value = to_number(value)
            rec[key] = value
        records.append(rec)

    return ExtractResult(records=records, items_found=len(records))


def run_extract(body: str, spec: Extract, base_url: str | None = None) -> ExtractResult:
    """Single entry point. ``body`` is the raw response text."""
    if spec.kind == "jsonld":
        from ..translate.jsonld import jsonld_records
        records = jsonld_records(body)
        return ExtractResult(records=records, items_found=len(records))
    if spec.kind == "json":
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return ExtractResult(records=[], items_found=0)
        return extract_from_json(parsed, spec)
    return extract_from_html(body, spec, base_url)
