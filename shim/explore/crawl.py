"""Stage 1: explore.

Fetches pages and reads their forms. Deliberately HTTP-only: the declared scope
is server-rendered HTML, and a browser buys exactly one thing (a JS-rendered
DOM) that this scope disclaims. ``BrowserExplorer`` adds that behind a flag.
"""

from __future__ import annotations

import asyncio
from collections import deque
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


def _safe_root_redirect(start_url: str, final_url: str) -> bool:
    """Allow canonical www aliases and HTTPS upgrades without expanding scope."""
    try:
        start, final = urlsplit(start_url), urlsplit(final_url)
        if (start.scheme not in {"http", "https"} or final.scheme not in {"http", "https"}
                or not start.hostname or not final.hostname
                or final.username is not None or final.password is not None
                or (start.scheme == "https" and final.scheme != "https")):
            return False
        if start.hostname.lower().removeprefix("www.") != final.hostname.lower().removeprefix("www."):
            return False
        start_default = 443 if start.scheme == "https" else 80
        final_default = 443 if final.scheme == "https" else 80
        start_port, final_port = start.port or start_default, final.port or final_default
        return ((start_port == start_default and final_port == final_default)
                or (start.scheme == final.scheme and start_port == final_port))
    except ValueError:
        return False


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
    concurrency: int | None = None,
) -> CrawlResult:
    """Breadth-first, bounded parallel downloads, ordered results, robots-aware."""
    from ..config import config
    cfg = config()
    concurrency = cfg.crawl_concurrency if concurrency is None else concurrency
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be between 1 and 16")
    parts = urlsplit(start_url)
    host = parts.netloc
    origin = f"{parts.scheme}://{host}"

    result = CrawlResult()
    robots = await _load_robots(origin) if cfg.respect_robots else None

    root_url = urldefrag(start_url)[0]
    queue = deque([root_url])
    seen: set[str] = set(queue)
    signatures: set[str] = set()

    pending: deque[tuple[str, asyncio.Task]] = deque()
    blocked = False

    async def fetch(url):
        nonlocal blocked
        try:
            res = await fetch_page(url)
            # Observe protection as soon as any download finishes, even if an
            # earlier BFS response is still pending. Never refill after that.
            if res.blocked:
                blocked = True
                current = asyncio.current_task()
                cutoff = next((index for index, (_, task) in enumerate(pending)
                               if task is current), -1)
                # Keep preceding BFS results, but cancel every later request
                # now, including requests still waiting on HTTP pacing.
                for _, task in list(pending)[cutoff + 1:]:
                    task.cancel()
            return res
        except Exception as exc:
            return exc

    try:
        while queue or pending:
            # Each pending request reserves one successful-page slot. This keeps
            # max_pages exact without speculative downloads beyond the budget.
            while (queue and not blocked and len(pending) < concurrency
                   and len(result.pages) + len(pending) < max_pages):
                url = queue.popleft()
                if robots and not robots.can_fetch(cfg.user_agent, url):
                    result.skips.append(Skip(what=url, reason="robots-disallowed"))
                    continue
                pending.append((url, asyncio.create_task(fetch(url))))
            if not pending:
                break

            # Consume in discovery order, but refill immediately after each page
            # instead of waiting for the slowest request in a whole batch.
            url, task = pending.popleft()
            res = await task
            if isinstance(res, Exception):
                result.pages_failed += 1
                result.skips.append(Skip(what=url, reason="fetch-error", detail=type(res).__name__))
                continue
            if res.blocked:
                result.blocked = True
                result.skips.append(Skip(what=url, reason="bot-protection", detail=f"HTTP {res.status}"))
                # Other bounded requests may be in flight; cancel them below.
                return result
            if res.status >= 400 or "html" not in res.content_type:
                result.pages_failed += 1
                continue

            if url == root_url:
                if not _safe_root_redirect(root_url, res.url):
                    result.skips.append(Skip(what=url, reason="not-useful", detail=f"redirect-out-of-scope: {res.url}"))
                    continue
                final = urlsplit(res.url)
                final_origin = f"{final.scheme}://{final.netloc}"
                if final_origin != origin:
                    # The first response establishes the canonical site before
                    # any links are queued or concurrent child fetches begin.
                    host, origin = final.netloc, final_origin
                    robots = await _load_robots(origin) if cfg.respect_robots else None
            elif not _same_site(res.url, host):
                result.skips.append(Skip(what=url, reason="not-useful", detail=f"redirect-out-of-scope: {res.url}"))
                continue
            if robots and not robots.can_fetch(cfg.user_agent, res.url):
                result.skips.append(Skip(what=res.url, reason="robots-disallowed"))
                continue
            seen.add(urldefrag(res.url)[0])

            soup = BeautifulSoup(res.body, "lxml")
            title_tag = soup.select_one("title")
            page = CrawledPage(url=res.url, html=res.body,
                               title=title_tag.get_text(strip=True) if title_tag else None)
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

    finally:
        for _, task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)

    return result
