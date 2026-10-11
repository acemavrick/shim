"""Link patterns as actions.

A site's links are its other half of callable surface: /ebooks/1342 and
/ebooks/84 are one capability, get_ebook(id), not two pages. Cluster same-site
links by path template, keep the clusters where exactly one segment varies,
and each becomes a detail-page tool with the observed values as examples.
"""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..manifest import Action, Extract, HttpBackend, InputSchema, PropertySchema, Source

MIN_MEMBERS = 4
NOISE = {"page", "tag", "tags", "category", "categories", "wiki", "search", "cdn", "static",
         "assets", "img", "images", "css", "js", "api", "feed", "rss", "login", "account"}


def _looks_like_id(values: list[str]) -> bool:
    """Identifiers, not section names: 1342, apex-knife-set, sku-88. Not: about,
    background, contact_information.html."""
    if any(v.lower().endswith((".html", ".htm", ".php", ".aspx")) for v in values):
        return False
    if all(v.isdigit() for v in values):
        return True
    rich = sum(1 for v in values if any(ch.isdigit() for ch in v) or "-" in v)
    return rich / len(values) >= 0.8


def _singular(noun: str) -> str:
    if noun.endswith("ies"):
        return noun[:-3] + "y"
    if noun.endswith("s") and not noun.endswith("ss"):
        return noun[:-1]
    return noun


def _slug(raw: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_"))[:30]


def collect_links(pages: list[tuple[str, str]], host: str) -> list[str]:
    out: set[str] = set()
    for url, html in pages:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href]"):
            href = a.get("href")
            if not isinstance(href, str) or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            try:
                resolved = urljoin(url, href)
            except ValueError:
                continue
            parts = urlsplit(resolved)
            if parts.netloc != host or parts.scheme not in ("http", "https"):
                continue
            out.add(f"{parts.scheme}://{parts.netloc}{parts.path}")
    return sorted(out)


def link_actions(links: list[str]) -> list[Action]:
    """Groups links by shape; one varying segment -> one tool."""
    groups: dict[tuple[int, tuple[tuple[int, str], ...]], list[list[str]]] = defaultdict(list)
    for link in links:
        parts = urlsplit(link)
        segs = [s for s in parts.path.split("/") if s]
        if not 1 <= len(segs) <= 4:
            continue
        for vary in range(len(segs)):
            fixed = tuple((i, s) for i, s in enumerate(segs) if i != vary)
            groups[(vary, fixed)].append(segs)

    actions: list[Action] = []
    seen_templates: set[str] = set()
    for (vary, fixed), members in groups.items():
        # Drop siblings that are section names, not identifiers: /ebooks/search/
        # sits beside /ebooks/1342 and must not veto the whole cluster.
        if vary == 0:
            continue  # /{x} is the site's section list, never a detail tool
        clean = [m for m in members if m[vary].lower() not in NOISE and len(m[vary]) <= 80
                 and not m[vary].lower().endswith((".html", ".htm", ".php", ".aspx"))]
        values = sorted({m[vary] for m in clean})
        if len(values) < MIN_MEMBERS or not _looks_like_id(values):
            continue
        if fixed and fixed[0][1].lower() in NOISE:
            continue
        sample = clean[0]
        template_segs = [("{id}" if i == vary else s) for i, s in enumerate(sample)]
        template = "/" + "/".join(template_segs)
        if template in seen_templates:
            continue
        seen_templates.add(template)

        noun = _singular(_slug(sample[vary - 1])) if vary > 0 else "page"
        if not noun:
            noun = "page"
        # Mostly-numeric clusters are numeric: drop the stray word so the
        # parameter is `id` and every example actually resolves.
        digits = [v for v in values if v.isdigit()]
        numeric = len(digits) >= 0.9 * len(values)
        if numeric:
            values = digits
        param = "id" if numeric else "slug"
        template = template.replace("{id}", "{" + param + "}")

        first = urlsplit(links[0])
        actions.append(Action(
            name=f"get_{noun}",
            description=f"Fetch one {noun} page on {first.netloc} by {param}. "
                        f"Observed {len(values)} of them.",
            tier="read",
            input=InputSchema(
                properties={param: PropertySchema(
                    type="string", description=f"{noun} {param}, e.g. {values[0]}",
                    examples=values[:6])},
                required=[param],
            ),
            backend=HttpBackend(method="GET", url=f"{first.scheme}://{first.netloc}{template}",
                                encoding="query", fields={}),
            extract=Extract(kind="css", fields={}),
            source=Source(page=f"{first.scheme}://{first.netloc}{template}", form_index=-1,
                          submit="link-pattern"),
        ))
    return actions
