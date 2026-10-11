"""Declared structured data: JSON-LD (schema.org) embedded in the page.

Everything else in `translate/` is inference -- guess a selector, fire two
queries, diff. This is the opposite: the site *states* its records, typed and
named, in a standard vocabulary. Where it exists it beats any selector, and it
survives redesigns that would break one. It is also the content half of the
spec -- "products, prices, hours" -- which forms alone never provide.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

# Types worth surfacing as records. Site chrome (BreadcrumbList, WebPage,
# SiteNavigationElement) is deliberately absent.
RECORD_TYPES = {
    "Product", "Offer", "Event", "Recipe", "Article", "NewsArticle", "BlogPosting",
    "Organization", "LocalBusiness", "Store", "Restaurant", "Person", "Place",
    "Book", "Movie", "Course", "JobPosting", "SoftwareApplication", "FAQPage",
    "Service", "WebSite",
}
# Nested objects whose scalar fields are worth lifting onto the record.
LIFT = {"offers", "brand", "address", "location", "aggregateRating", "author",
        "openingHoursSpecification", "potentialAction"}
MAX_FIELDS = 16
_STRIP_HTML = re.compile(r"<[^>]+>")


def _flatten_graph(data: Any) -> list[dict[str, Any]]:
    """Unnests @graph and top-level arrays into a flat list of typed nodes."""
    if isinstance(data, list):
        return [n for item in data for n in _flatten_graph(item)]
    if not isinstance(data, dict):
        return []
    if "@graph" in data and isinstance(data["@graph"], list):
        return _flatten_graph(data["@graph"])
    return [data]


def _type_of(node: dict[str, Any]) -> str | None:
    t = node.get("@type")
    if isinstance(t, list):
        t = next((x for x in t if isinstance(x, str)), None)
    return t if isinstance(t, str) else None


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = _STRIP_HTML.sub(" ", value)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:300] if text else None
    if isinstance(value, list):
        scalars = [s for s in (_scalar(v) for v in value) if isinstance(s, (str, int, float))]
        return scalars[:8] or None
    return None


def _number(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", value.replace(",", ""))
        if m:
            n = float(m.group(0))
            return int(n) if n.is_integer() else n
    return value


def _lift(prefix: str, obj: dict[str, Any], out: dict[str, Any]) -> None:
    for key, value in obj.items():
        if key.startswith("@") or len(out) >= MAX_FIELDS:
            continue
        scalar = _scalar(value)
        if scalar is None:
            continue
        name = key if prefix in ("offers",) and key in ("price", "priceCurrency", "availability", "sku") \
            else f"{prefix}_{key}"
        if key in ("price", "lowPrice", "highPrice", "ratingValue", "reviewCount"):
            scalar = _number(scalar)
        if key == "availability" and isinstance(scalar, str):
            scalar = scalar.rsplit("/", 1)[-1]  # https://schema.org/InStock -> InStock
        out.setdefault(name, scalar)


def to_record(node: dict[str, Any]) -> dict[str, Any] | None:
    """One schema.org node -> one flat record. None if it is not a record type."""
    rtype = _type_of(node)
    if rtype not in RECORD_TYPES:
        return None

    record: dict[str, Any] = {"type": rtype}
    for key, value in node.items():
        if key.startswith("@") or len(record) >= MAX_FIELDS:
            continue
        if key in LIFT:
            nested = value[0] if isinstance(value, list) and value else value
            if isinstance(nested, dict):
                _lift(key, nested, record)
            elif isinstance(nested, str):
                record.setdefault(key, _scalar(nested))
            continue
        scalar = _scalar(value)
        if scalar is not None:
            record[key] = scalar

    # A record with only a type is noise.
    return record if len(record) > 1 else None


def record_key(record: dict[str, Any]) -> tuple[str, str]:
    """Identity for de-duplication: a site repeats its Organization/Person
    block on every page, and that is one record, not one per page."""
    ident = record.get("sku") or record.get("url") or record.get("name") or ""
    return (record["type"], str(ident).lower())


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in records:
        key = record_key(r)
        if key[1] and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def jsonld_records(html: str) -> list[dict[str, Any]]:
    """Every declared record on a page, flattened."""
    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            # Some themes emit trailing commas or comments; one cheap repair.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except ValueError:
                continue
        for node in _flatten_graph(data):
            record = to_record(node)
            if record:
                records.append(record)
    return records
