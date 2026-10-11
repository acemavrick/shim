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
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..manifest import Extract

URL_ATTRS = {"href", "src", "action", "poster", "data-href"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")


@dataclass
class ExtractResult:
    records: list[dict[str, Any]]
    items_found: int


@dataclass
class FieldSpec:
    selector: str
    attr: str | None = None
    cast: str | None = None


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


def extract_from_html(html: str, spec: Extract, base_url: str | None = None) -> ExtractResult:
    soup = BeautifulSoup(html, "lxml")
    specs = {k: parse_field_spec(v) for k, v in spec.fields.items()}

    # No `items` selector means the whole document is one record.
    try:
        roots: list[Tag | None] = list(soup.select(spec.items)) if spec.items else [None]
    except Exception:
        roots = []

    records: list[dict[str, Any]] = []
    for root in roots:
        rec: dict[str, Any] = {}
        for key, f in specs.items():
            scope = root if root is not None else soup
            if f.selector == "":
                node = scope if isinstance(scope, Tag) else None
            else:
                try:
                    node = scope.select_one(f.selector)
                except Exception:
                    node = None
            rec[key] = _finish(_read(node, f), f, base_url) if node is not None else None
        records.append(rec)

    return ExtractResult(records=records, items_found=len(records))


def _at_path(root: Any, path: str) -> Any:
    if path == "":
        return root
    cur = root
    for seg in path.split("."):
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


def extract_from_json(body: Any, spec: Extract) -> ExtractResult:
    target = _at_path(body, spec.items) if spec.items else body
    items = target if isinstance(target, list) else [target]

    records: list[dict[str, Any]] = []
    for item in items:
        rec: dict[str, Any] = {}
        for key, raw in spec.fields.items():
            f = parse_field_spec(raw)
            value = _at_path(item, f.selector)
            if f.cast == "number" and isinstance(value, str):
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
