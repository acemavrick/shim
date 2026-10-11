"""Stage 1: explore.

Fetches pages and reads their forms. Deliberately HTTP-only: the declared scope
is server-rendered HTML, and a browser buys exactly one thing (a JS-rendered
DOM) that this scope disclaims. ``BrowserExplorer`` adds that behind a flag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ..manifest import Skip
from ..runtime.http import USER_AGENT, fetch_page
from ..translate.forms import FormDescriptor, parse_forms

SKIP_EXTENSIONS = {
    ".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".xml", ".rss", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
}


@dataclass
class CrawledPage:
    url: str
    html: str
    title: str | None = None


@dataclass
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    forms: list[FormDescriptor] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    pages_failed: int = 0
    blocked: bool = False


async def _load_robots(origin: str) -> RobotFileParser | None:
    parser = RobotFileParser()
    try:
        res = await fetch_page(urljoin(origin, "/robots.txt"))
    except Exception:
        return None
    if res.status != 200:
        return None
    parser.parse(res.body.splitlines())
    return parser


def _same_site(candidate: str, host: str) -> bool:
    parts = urlsplit(candidate)
    return parts.scheme in ("http", "https") and parts.netloc == host


def _worth_fetching(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def _links(html: str, page_url: str, host: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not isinstance(href, str) or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            resolved, _ = urldefrag(urljoin(page_url, href))
        except ValueError:
            continue
        if _same_site(resolved, host) and _worth_fetching(resolved):
            out.append(resolved)
    return out


async def crawl(
    start_url: str,
    max_pages: int = 12,
    on_page: "callable | None" = None,
) -> CrawlResult:
    """Breadth-first, same-site, serial, robots-aware."""
    parts = urlsplit(start_url)
    host = parts.netloc
    origin = f"{parts.scheme}://{host}"

    result = CrawlResult()
    robots = await _load_robots(origin)

    queue: list[str] = [urldefrag(start_url)[0]]
    seen: set[str] = set(queue)
    signatures: set[str] = set()

    while queue and len(result.pages) < max_pages:
        url = queue.pop(0)

        if robots and not robots.can_fetch(USER_AGENT, url):
            result.skips.append(Skip(what=url, reason="robots-disallowed"))
            continue

        try:
            res = await fetch_page(url)
        except Exception as exc:
            result.pages_failed += 1
            result.skips.append(
                Skip(what=url, reason="fetch-error", detail=type(exc).__name__)
            )
            continue

        if res.blocked:
            result.blocked = True
            result.skips.append(
                Skip(what=url, reason="bot-protection", detail=f"HTTP {res.status}")
            )
            # A defended site will not yield to retries; stop politely.
            break

        if res.status >= 400 or "html" not in res.content_type:
            result.pages_failed += 1
            continue

        soup = BeautifulSoup(res.body, "lxml")
        title_tag = soup.select_one("title")
        page = CrawledPage(
            url=res.url,
            html=res.body,
            title=title_tag.get_text(strip=True) if title_tag else None,
        )
        result.pages.append(page)

        for descriptor in parse_forms(res.body, res.url):
            # A site-wide header form appears on every page; keep the first.
            if descriptor.signature in signatures:
                continue
            signatures.add(descriptor.signature)
            result.forms.append(descriptor)

        if on_page:
            maybe = on_page(page, result)
            if asyncio.iscoroutine(maybe):
                await maybe

        for link in _links(res.body, res.url, host):
            if link not in seen and len(seen) < max_pages * 6:
                seen.add(link)
                queue.append(link)

    return result
