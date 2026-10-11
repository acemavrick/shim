"""Browser-backed exploration: the Tier-3 path.

Adds two things HTTP-only crawling cannot reach:

1. **Rendered forms.** A form that React builds at runtime does not exist in the
   served HTML, so `parse_forms` never sees it. Here it does.
2. **Observed endpoints.** An SPA's real capability is usually a JSON API its
   own JavaScript calls. Watching the network while the page loads captures
   those directly -- which is the spec's request-recording idea, minus the
   diffing, because a captured URL already shows its parameters by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from ..manifest import Skip
from ..runtime.http import USER_AGENT
from ..translate.forms import FormDescriptor, parse_forms
from .crawl import CrawledPage, CrawlResult, _links, _worth_fetching

# Values this long with no spaces are session tokens or cache-busters, not
# parameters an agent should be asked to invent.
OPAQUE_VALUE_LENGTH = 24
IGNORED_PARAMS = {"_", "t", "ts", "timestamp", "cb", "nonce", "_ga", "sig"}


@dataclass
class ObservedEndpoint:
    url: str
    method: str
    # param name -> observed value
    params: dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    sample: str = ""

    @property
    def signature(self) -> str:
        parts = urlsplit(self.url)
        return f"{self.method} {parts.netloc}{parts.path} {','.join(sorted(self.params))}"


@dataclass
class BrowserResult(CrawlResult):
    endpoints: list[ObservedEndpoint] = field(default_factory=list)


async def browser_crawl(
    start_url: str,
    max_pages: int = 8,
    on_page=None,
) -> BrowserResult:
    from playwright.async_api import async_playwright

    host = urlsplit(start_url).netloc
    result = BrowserResult()
    seen_endpoints: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        async def on_response(response) -> None:
            try:
                request = response.request
                if request.method != "GET" or response.status != 200:
                    return
                if request.resource_type not in ("xhr", "fetch"):
                    return
                content_type = (response.headers or {}).get("content-type", "")
                if "json" not in content_type:
                    return

                parts = urlsplit(request.url)
                params = {
                    k: v for k, v in parse_qsl(parts.query)
                    if k not in IGNORED_PARAMS
                }
                endpoint = ObservedEndpoint(
                    url=request.url, method="GET", params=params,
                    content_type=content_type,
                )
                if endpoint.signature in seen_endpoints:
                    return
                seen_endpoints.add(endpoint.signature)

                try:
                    endpoint.sample = (await response.text())[:200_000]
                except Exception:
                    return
                result.endpoints.append(endpoint)
            except Exception:
                return  # never let instrumentation break the crawl

        page.on("response", on_response)

        queue = [start_url]
        visited: set[str] = set()

        while queue and len(result.pages) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                # Give client-side rendering and its first data fetches a moment.
                try:
                    await page.wait_for_load_state("networkidle", timeout=6_000)
                except Exception:
                    pass
                html = await page.content()
                title = await page.title()
            except Exception as exc:
                result.pages_failed += 1
                result.skips.append(
                    Skip(what=url, reason="fetch-error", detail=type(exc).__name__)
                )
                continue

            crawled = CrawledPage(url=page.url, html=html, title=title)
            result.pages.append(crawled)

            signatures = {f.signature for f in result.forms}
            for descriptor in parse_forms(html, page.url):
                if descriptor.signature not in signatures:
                    result.forms.append(descriptor)

            if on_page:
                maybe = on_page(crawled, result)
                if hasattr(maybe, "__await__"):
                    await maybe

            for link in _links(html, page.url, host):
                if link not in visited and _worth_fetching(link):
                    queue.append(link)

        await browser.close()

    return result
