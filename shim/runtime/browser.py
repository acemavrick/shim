"""Fixed browser recipes. Fresh sessions, bounded execution, no model or supplied code."""
import asyncio
import json
import re
from urllib.parse import parse_qsl, quote, urlsplit

from ..config import config
from ..safety import post_is_approved
from .http import RawResponse


def substitute(value, args, url=False):
    def replace(match):
        name = match.group(1)
        if name not in args or args[name] is None:
            raise ValueError(f"Missing browser input: {name}")
        text = str(args[name])
        return quote(text, safe="") if url else text
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", replace, value or "")


async def replay_browser(backend, args):
    from playwright.async_api import async_playwright
    start = substitute(backend.start_url, args, url=True)
    origin = urlsplit(start)
    if origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password:
        raise ValueError("Invalid browser start URL")
    denied = []
    async def guard(route):
        request = route.request
        parts = urlsplit(request.url)
        same_origin = (parts.scheme, parts.netloc) == (origin.scheme, origin.netloc)
        # Third-party assets are not needed to prove a capability; never execute their requests.
        approved = same_origin and request.method in {"GET", "HEAD", "OPTIONS"}
        if same_origin and request.method == "POST":
            try:
                body = request.post_data_json
            except Exception:
                body = dict(parse_qsl(request.post_data or ""))
            approved = post_is_approved(request.url, body, config().read_post_rules)
        if approved:
            await route.continue_()
        else:
            if request.resource_type in {"document", "xhr", "fetch"}:
                denied.append(f"{request.method} {request.url}")
            await route.abort()

    async with asyncio.timeout(45):
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                await context.route("**/*", guard)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                page = await context.new_page()
                page.set_default_timeout(5000)
                response = await page.goto(start, wait_until="domcontentloaded", timeout=15000)
                status = response.status if response else 200
                for step in backend.steps:
                    if step.action == "goto":
                        target = substitute(step.url, args, url=True)
                        parts = urlsplit(target)
                        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc):
                            raise ValueError("Browser recipe cannot navigate outside its origin")
                        response = await page.goto(target, wait_until="domcontentloaded", timeout=15000)
                        status = response.status if response else status
                    elif step.action == "back":
                        response = await page.go_back(wait_until="domcontentloaded")
                        status = response.status if response else status
                    elif step.action == "scroll":
                        await page.mouse.wheel(0, 900)
                    else:
                        name = substitute(step.name, args)
                        if not name:
                            raise ValueError("Browser control requires a name")
                        locator = page.get_by_role(step.role, name=name, exact=True) if step.role else page.get_by_label(name, exact=True)
                        if await locator.count() != 1:
                            raise ValueError(f"Browser control must match exactly once: {name}")
                        text = substitute(step.text, args)
                        if step.action == "click":
                            await locator.click()
                        elif step.action == "fill":
                            await locator.fill(text)
                        elif step.action == "select":
                            await locator.select_option(label=text)
                        elif step.action == "press":
                            if text not in {"Enter", "Tab", "Escape"}:
                                raise ValueError("Unsupported browser key")
                            await locator.press(text)
                    await page.wait_for_timeout(250)
                if denied:
                    raise ValueError("Browser recipe required a blocked request: " + denied[0])
                body = await page.content()
                return RawResponse(body=body, content_type="text/html", status=status,
                                   url=page.url, bytes=len(body.encode()))
            finally:
                await browser.close()
