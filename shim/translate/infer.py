"""Infers an extractor from a real response.

At translate time the response shape is unknown -- a form tells you how to ask,
not what comes back. So the compiler fires one real request and reads the
structure off the answer. This is the difference between a tool that returns
``[]`` and one that returns products.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..manifest import Extract

_NUMERIC = re.compile(r"-?\d+(?:[.,]\d+)?")
# A price is a currency marker next to a number, or a number with two decimals.
# Markers cover symbols and the codes/words non-US shops print (Rs., лв, INR...).
PRICE = re.compile(
    r"(?:[$£€¥₹₽₩]\s*\d"
    r"|\b(?:Rs\.?|USD|EUR|GBP|INR|BGN|CAD|AUD|JPY|CHF|лв\.?|руб\.?)\s*\d"
    r"|\d\s*(?:€|£|лв\.?|руб\.?|USD|EUR|GBP|BGN)\b"
    r"|(?<!\d)\d+[.,]\d{2}(?!\d))",
    re.I,
)
MIN_REPEATS = 2
NOISE_CLASSES = {"row", "col", "container", "wrapper", "clearfix", "hidden"}
# Characters that make a class name unusable as a bare CSS selector. Utility
# classes (md:flex, w-1/2, p-[3px]) carry these, and they are layout, never the
# record container, so skipping them loses nothing.
_UNSAFE_CLASS = re.compile(r"[^A-Za-z0-9_-]")


def _safe_select(scope: Any, selector: str) -> list[Tag]:
    """select() that treats an invalid selector as "no match" instead of crashing."""
    try:
        return [t for t in scope.select(selector) if isinstance(t, Tag)]
    except Exception:
        return []


def _safe_select_one(scope: Any, selector: str) -> Tag | None:
    try:
        node = scope.select_one(selector)
    except Exception:
        return None
    return node if isinstance(node, Tag) else None

# A page's real answer lives in its main content. Site chrome repeats far more
# consistently than results do, so an unscoped search reliably picks the
# sidebar over the thing the user asked for.
MAIN_SELECTORS = (
    "main", "[role=main]", "#mw-content-text", "#content", "#main-content",
    "#main", "#primary", "article", ".content", ".results", ".search-results",
)
CHROME_TAGS = {"nav", "header", "footer", "aside"}
CHROME_ROLES = {"navigation", "banner", "contentinfo", "search", "complementary"}
# Sites that skip semantic tags still name their chrome.
CHROME_CLASS = re.compile(r"(^|[-_ ])(nav|navbar|navigation|menu|breadcrumbs?|footer|header|sidebar|topbar|cookie)([-_ ]|$)", re.I)


def _content_root(soup: BeautifulSoup) -> tuple[Tag | BeautifulSoup, str]:
    """Narrows the search to the page's main content, if it declares one."""
    for selector in MAIN_SELECTORS:
        node = soup.select_one(selector)
        if node is not None and len(node.get_text(strip=True)) > 200:
            return node, selector
    return soup, ""


def _in_chrome(el: Tag) -> bool:
    """True if the element sits inside navigation, a header, footer or sidebar."""
    for parent in [el, *el.parents]:
        if not isinstance(parent, Tag):
            continue
        if parent.name in CHROME_TAGS:
            return True
        role = parent.get("role")
        if isinstance(role, str) and role.lower() in CHROME_ROLES:
            return True
        if any(CHROME_CLASS.search(c) for c in _classes(parent)):
            return True
        pid = parent.get("id")
        if isinstance(pid, str) and CHROME_CLASS.search(pid):
            return True
    return False


def _classes(el: Tag) -> list[str]:
    raw = el.get("class")
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, str)]
    return [raw] if isinstance(raw, str) else []


def _distinctive_class(el: Tag) -> str | None:
    for c in _classes(el):
        if len(c) < 2 or c.isdigit() or c.lower() in NOISE_CLASSES:
            continue
        if _UNSAFE_CLASS.search(c) or c[0].isdigit() or c[0] == "-":
            continue
        return c
    return None


def _selector_for(item_root: Tag, node: Tag) -> str | None:
    """Shortest selector that finds `node` inside `item_root`."""
    cls = _distinctive_class(node)
    if cls:
        candidate = f".{cls}"
        if _safe_select_one(item_root, candidate) is node:
            return candidate
    if _safe_select_one(item_root, node.name) is node:
        return node.name
    if cls:
        return f".{cls}"
    return None


def infer_listing(html: str) -> Extract:
    """Finds the repeating record structure on a results page."""
    soup = BeautifulSoup(html, "lxml")
    root, root_selector = _content_root(soup)
    scope = f"{root_selector} " if root_selector else ""

    # Candidate containers: elements sharing a distinctive class, appearing often,
    # each holding a link and some text -- and not part of the site chrome.
    counts: Counter[str] = Counter()
    for el in root.find_all(True):
        if not isinstance(el, Tag) or _in_chrome(el):
            continue
        cls = _distinctive_class(el)
        # No anchor requirement: a table of records has links in none of its
        # rows, and requiring one picks a layout div over the actual data.
        if cls and el.find(True) is not None and len(el.get_text(strip=True)) > 10:
            counts[cls] += 1

    best_selector: str | None = None
    best_score = 0.0
    for cls, count in counts.items():
        if count < MIN_REPEATS:
            continue
        members = [m for m in _safe_select(root, f".{cls}") if not _in_chrome(m)]
        if not members:
            continue
        avg_text = sum(len(m.get_text(strip=True)) for m in members) / len(members)
        # Favour many items of moderate size over one giant wrapper.
        score = count * min(avg_text, 400)
        if score > best_score:
            best_score, best_selector = score, f"{scope}.{cls}"

    if not best_selector:
        return _single_record(soup)

    items = _safe_select(soup, best_selector)
    if not items:
        return _single_record(soup)
    return Extract(kind="css", items=best_selector, fields=_fields_for(items[0]))


def _fields_for(item_root: Tag) -> dict[str, str]:
    """Reads a record's shape off one example item.

    Two record shapes dominate the web: a card with a title link, and a table
    row whose cells are classed. They need different treatment -- a card's
    identity is its anchor text, a row's is spread across its cells.
    """
    anchor = next(
        (a for a in item_root.find_all("a") if isinstance(a, Tag) and a.get_text(strip=True)),
        None,
    )
    heading = item_root.find(["h1", "h2", "h3", "h4"])
    titled = (isinstance(heading, Tag) and heading.get_text(strip=True)) or anchor is not None

    if not titled:
        return _cell_fields(item_root)

    fields: dict[str, str] = {}
    if isinstance(heading, Tag) and heading.get_text(strip=True):
        sel = _selector_for(item_root, heading)
        if sel:
            fields["name"] = sel
    if anchor is not None:
        if "name" not in fields:
            sel = _selector_for(item_root, anchor)
            if sel:
                fields["name"] = sel
        href_sel = _selector_for(item_root, anchor)
        if href_sel:
            fields["url"] = f"{href_sel}@href"

    # Anything that looks like a price becomes a numeric field.
    for el in item_root.find_all(True):
        if not isinstance(el, Tag) or el.find(True) is not None:
            continue  # leaf nodes only
        text = el.get_text(strip=True)
        if text and PRICE.search(text):
            sel = _selector_for(item_root, el)
            if sel and sel not in fields.values():
                fields["price"] = f"{sel}|number"
                break

    return fields or _cell_fields(item_root)


def _cell_fields(item_root: Tag, limit: int = 6) -> dict[str, str]:
    """Builds fields from classed leaf cells, as a table row carries them."""
    fields: dict[str, str] = {}
    for el in item_root.find_all(True):
        if not isinstance(el, Tag) or el.find(True) is not None:
            continue
        cls = _distinctive_class(el)
        text = el.get_text(strip=True)
        if not cls or not text or len(text) > 120:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", cls.lower()).strip("_")
        if not key or key in fields:
            continue
        sel = _selector_for(item_root, el)
        if sel:
            fields[key] = f"{sel}|number" if _NUMERIC.fullmatch(text) else sel
        if len(fields) >= limit:
            break

    if not fields:
        fields["text"] = ""
    return fields


def _single_record(soup: BeautifulSoup) -> Extract:
    """No repeating structure: return the page's own identity rather than nothing."""
    fields: dict[str, str] = {}
    if soup.select_one("h1"):
        fields["heading"] = "h1"
    if soup.select_one("title"):
        fields["title"] = "title"
    if not fields:
        fields["text"] = "body"
    return Extract(kind="css", fields=fields)


def infer_json(body: Any) -> Extract:
    """For endpoints that already return JSON.

    Finds the array of records wherever it lives rather than guessing at a
    fixed set of key names -- every API names its collection differently
    ("quotes", "packages", "docs"), and a hardcoded list silently drops the
    ones it has not heard of.
    """
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return Extract(kind="json", fields={k: k for k in list(body[0])[:8]})

    if isinstance(body, dict):
        # Prefer conventional names, then any key holding a list of objects.
        preferred = ("results", "items", "data", "products", "hits", "records", "docs")
        candidates = [k for k in preferred if k in body] + [
            k for k in body if k not in preferred
        ]
        for key in candidates:
            value = body.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return Extract(
                    kind="json", items=key,
                    fields={k: k for k in list(value[0])[:8]},
                )
        # A single object response is still one record.
        scalars = {
            k: k for k, v in body.items()
            if isinstance(v, (str, int, float, bool))
        }
        if scalars:
            return Extract(kind="json", fields=dict(list(scalars.items())[:8]))

    return Extract(kind="json", fields={})


def _candidate_members(html: str) -> tuple[dict[str, list[Tag]], str]:
    """Distinctive classes in the main content, with their members."""
    soup = BeautifulSoup(html, "lxml")
    root, root_selector = _content_root(soup)

    names: set[str] = set()
    for el in root.find_all(True):
        if not isinstance(el, Tag) or _in_chrome(el):
            continue
        cls = _distinctive_class(el)
        if cls and len(el.get_text(strip=True)) > 10:
            names.add(cls)

    out: dict[str, list[Tag]] = {}
    for cls in names:
        members = [m for m in _safe_select(root, f".{cls}") if not _in_chrome(m)]
        if len(members) >= MIN_REPEATS:
            out[cls] = members
    return out, root_selector


def _texts(members: list[Tag]) -> set[str]:
    return {" ".join(m.get_text(" ", strip=True).split())[:160] for m in members}


def infer_by_diff(html_a: str, html_b: str) -> Extract | None:
    """Picks the extractor by diffing two responses to different inputs.

    This is the spec's request-diffing idea applied where it actually pays:
    rather than asking which parts of a *request* are parameters, ask which
    parts of the *response* changed when the input changed. Navigation,
    headers and sidebars are identical across both; the results are not.
    Whatever moved is the answer.
    """
    cand_a, scope_selector = _candidate_members(html_a)
    cand_b, _ = _candidate_members(html_b)
    scope = f"{scope_selector} " if scope_selector else ""

    best: tuple[float, str, Tag] | None = None
    for cls, members_a in cand_a.items():
        members_b = cand_b.get(cls)
        if not members_b:
            continue

        texts_a, texts_b = _texts(members_a), _texts(members_b)
        union = texts_a | texts_b
        if not union:
            continue
        churn = 1.0 - (len(texts_a & texts_b) / len(union))

        # Identical across both probes means page furniture, not results.
        if churn < 0.4:
            continue

        avg_text = sum(len(t) for t in texts_a) / max(len(texts_a), 1)
        score = churn * len(members_a) * min(avg_text, 300)
        if best is None or score > best[0]:
            best = (score, f"{scope}.{cls}", members_a[0])

    if best is None:
        return None
    return Extract(kind="css", items=best[1], fields=_fields_for(best[2]))


def prune_empty_fields(
    spec: Extract, records: list[dict[str, Any]], threshold: float = 0.6
) -> Extract:
    """Drops inferred fields that are mostly null against a real response.

    Inference guesses a field from the first item; the guess may not hold for
    the rest (headings with no anchor, cards with no price). A column that is
    almost always null is noise in every tool result, so it is removed -- but
    never the last surviving field.
    """
    if not records or len(spec.fields) <= 1:
        return spec

    keep: dict[str, str] = {}
    for name, selector in spec.fields.items():
        filled = sum(1 for r in records if r.get(name) not in (None, "", []))
        if filled / len(records) >= (1 - threshold):
            keep[name] = selector

    if not keep:
        return spec
    return spec.model_copy(update={"fields": keep})


def detail_extract(html: str) -> Extract:
    """A detail page is one record, not a listing: its identity is the title,
    the main heading, a price if any, and the meta description."""
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    if soup.select_one("title"):
        fields["title"] = "title"
    for sel in ("h1", "h2"):
        node = soup.select_one(sel)
        if node is not None and node.get_text(strip=True):
            fields["heading"] = sel
            break
    for el in soup.find_all(True):
        if not isinstance(el, Tag) or el.find(True) is not None or _in_chrome(el):
            continue
        text = el.get_text(strip=True)
        if text and PRICE.search(text) and len(text) < 40:
            sel = _distinctive_class(el)
            if sel:
                fields["price"] = f".{sel}|number"
                break
    if soup.select_one('meta[name="description"]'):
        fields["description"] = 'meta[name="description"]@content'
    if soup.select_one("meta[property='og:image']"):
        fields["image"] = "meta[property='og:image']@content"
    return Extract(kind="css", fields=fields or {"text": "body"})
