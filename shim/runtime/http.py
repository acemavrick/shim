"""Fires an action's HTTP request.

Shared by ``check`` and ``serve`` -- the whole point of the split. A template
that passes a check here and fails at serve time is the exact failure mode this
single implementation exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any
import json
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx
from bs4 import BeautifulSoup, SoupStrainer

from ..manifest import Action, HttpBackend
from .extract import ExtractResult, run_extract

# httpx logs every request at INFO; stdio transports must keep the stream clean.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

USER_AGENT = "shim-compiler/0.1 (site-to-agent translation layer)"
ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"

# Space request starts per origin, while allowing downloads to overlap.
# Loop-local locks also support successive asyncio.run() invocations.
_pacers: WeakKeyDictionary = WeakKeyDictionary()


@dataclass
class _Pacer:
    lock: asyncio.Lock
    last_request_at: float = 0.0

# Cookie jar. One process == one session under stdio.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        from ..config import config
        _client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            headers={"user-agent": config().user_agent, "accept": ACCEPT},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def reset_jar() -> None:
    if _client is not None:
        _client.cookies.clear()


@dataclass
class RawResponse:
    body: str
    content_type: str
    status: int
    url: str
    bytes: int

    @property
    def blocked(self) -> bool:
        """Heuristic for Tier-4 defences, so coverage can say why we stopped."""
        if self.status in (403, 429, 503):
            return True
        lowered = self.body[:2000].lower()
        return any(
            marker in lowered
            for marker in ("just a moment", "cf-browser-verification", "captcha", "are you a robot")
        )


async def _polite_fetch(method: str, url: str, **kwargs: Any) -> RawResponse:
    from ..config import config
    parts = urlsplit(url)
    origin = (parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    pacers = _pacers.setdefault(asyncio.get_running_loop(), {})
    pacer = pacers.setdefault(origin, _Pacer(asyncio.Lock()))
    async with pacer.lock:
        gap = time.monotonic() - pacer.last_request_at
        minimum = config().min_request_gap
        if gap < minimum:
            await asyncio.sleep(minimum - gap)
        pacer.last_request_at = time.monotonic()

    res = await _get_client().request(method, url, **kwargs)
    body = res.text
    return RawResponse(
        body=body,
        content_type=res.headers.get("content-type", ""),
        status=res.status_code,
        url=str(res.url),
        bytes=len(body.encode()),
    )


async def fetch_page(url: str) -> RawResponse:
    return await _polite_fetch("GET", url)


async def _refetch_hidden_fields(url: str) -> dict[str, str]:
    """Re-scrapes hidden fields so rotating tokens are current, not compile-time stale."""
    res = await _polite_fetch("GET", url)
    # Only input attributes are used here; avoid building the entire document's
    # tree on every rotating-token refresh.
    soup = BeautifulSoup(res.body, "lxml", parse_only=SoupStrainer("input"))
    out: dict[str, str] = {}
    for el in soup.select("input[type=hidden][name]"):
        name = el.get("name")
        if isinstance(name, str):
            value = el.get("value")
            out[name] = value if isinstance(value, str) else ""
    return out


def resolve_fields(
    backend: HttpBackend,
    args: dict[str, Any],
    page_values: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    params: list[tuple[str, Any]] = []
    for wire, src in backend.fields.items():
        if src.from_ == "const":
            params.append((wire, src.value))
        elif src.from_ == "generate":
            params.append((wire, str(uuid.uuid4()) if src.kind == "uuid"
                           else str(int(time.time() * 1000))))
        elif src.from_ == "input":
            value = args.get(src.name)
            # Omit absent optionals rather than sending empty strings, which some
            # servers treat as an explicit empty filter.
            if value is None or value == "":
                continue
            if backend.encoding == "json":
                params.append((wire, value))
                continue
            if isinstance(value, bool):
                # Unchecked checkboxes are omitted entirely, as a browser would.
                if value:
                    params.append((wire, src.true_value or "on"))
                continue
            params.append((wire, str(value)))
        else:
            value = (page_values or {}).get(src.field)
            if value is not None:
                params.append((wire, value))
    return params


async def send_http(backend: HttpBackend, args: dict[str, Any], *, require_read_policy: bool = False) -> RawResponse:
    page_values = None
    if backend.refetch_from and any(f.from_ == "page" for f in backend.fields.values()):
        page_values = await _refetch_hidden_fields(backend.refetch_from)

    params = resolve_fields(backend, args, page_values)

    # Path placeholders: /ebooks/{id} -> /ebooks/1342. Missing ones are left
    # as-is so the server's 404 explains itself instead of a KeyError here.
    target = backend.url
    if "{" in target:
        for key, value in args.items():
            target = target.replace("{" + key + "}", quote(str(value), safe=""))

    if backend.method == "GET":
        # The HTML form submission algorithm REPLACES the action's query string:
        # action="/search?page=2" submitting q=socks yields "/search?q=socks".
        parts = urlsplit(target)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return await _polite_fetch("GET", url, params=params, headers=backend.headers)

    if require_read_policy:
        from ..config import config
        from ..safety import post_is_approved
        if not post_is_approved(target, dict(params), config().read_post_rules):
            raise ValueError("Read POST is not approved by operator policy")

    # Encode the body ourselves: httpx treats a non-dict `data=` as a byte
    # stream, which on an AsyncClient fails as "sync request", and a dict
    # would collapse repeated field names that forms legitimately send.
    if backend.encoding == "json":
        body, content_type = json.dumps(dict(params)), "application/json"
    else:
        body = urlencode([(k, str(v)) for k, v in params])
        content_type = "application/x-www-form-urlencoded"
    # Browsers send Origin and Referer on every POST, and same-origin checks
    # are the most common reason a replayed form gets a 403.
    parts = urlsplit(target)
    origin = f"{parts.scheme}://{parts.netloc}"
    headers = {"origin": origin, "referer": backend.refetch_from or origin + "/",
               **backend.headers, "content-type": content_type}
    # A 307/308 must never replay an authorized body to an unauthorized target.
    return await _polite_fetch("POST", target, content=body, headers=headers, follow_redirects=False)


@dataclass
class CallResult:
    records: list[dict[str, Any]]
    items_found: int
    status: int
    bytes: int
    url: str
    blocked: bool = False


async def invoke_action(action: Action, args: dict[str, Any]) -> CallResult:
    """The single path an action takes, for both ``check`` and ``serve``.

    Tier enforcement lives in the server, not here -- this layer always obeys.
    """
    if action.backend.kind == "browser":
        if action.tier != "read":
            raise ValueError("Browser recipes are read-only")
        from .browser import replay_browser
        res = await replay_browser(action.backend, args)
    elif action.backend.kind == "http":
        if action.tier == "read" and action.backend.method == "POST":
            res = await send_http(action.backend, args, require_read_policy=True)
        else:
            res = await send_http(action.backend, args)
    else:
        raise NotImplementedError(
            f'action "{action.name}" uses the {action.backend.kind} backend, '
            "which this build does not implement"
        )

    extracted: ExtractResult = run_extract(res.body, action.extract, res.url)
    return CallResult(
        records=extracted.records,
        items_found=extracted.items_found,
        status=res.status,
        bytes=res.bytes,
        url=res.url,
        blocked=res.blocked,
    )
