"""Bounded, read-only browser evidence. Never runs model-supplied code."""

from ..translate.describe import _trim_html


async def inspect_page(page, tool: str, *, selector: str = "main", observed=(), console=()):
    if tool == "network":
        return {"requests": [{"method": o.method, "url": o.url[:500], "status": o.status,
                              "content_type": o.content_type[:100],
                              "sample": _trim_html(o.sample, budget=120)}
                             for o in list(observed)[-12:]]}
    if tool == "console":
        return {"messages": [str(m)[:300] for m in list(console)[-12:]]}
    if tool not in {"dom", "links"}:
        return {"error": "Unknown inspection tool. Use dom, links, network, or console."}
    try:
        if tool == "links":
            return {"links": await page.locator("a[href]").evaluate_all(
                "xs => xs.slice(0, 40).map(a => ({name:a.innerText.slice(0,120),url:a.href.slice(0,500)}))")}
        if len(selector) > 200:
            return {"error": "Selector exceeds 200 characters."}
        loc = page.locator(selector)
        count = await loc.count()
        fragments = [await loc.nth(i).evaluate("e => e.outerHTML.slice(0, 6000)")
                     for i in range(min(count, 3))]
        return {"selector": selector, "matches": count,
                "html": _trim_html("\n".join(fragments), budget=6000)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: inspection failed; try a different selector."}
