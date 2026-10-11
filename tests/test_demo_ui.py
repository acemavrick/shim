import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


def test_history_ui_binds_old_version_calls_and_artifacts():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            calls = []
            action = {"name": "list_projects", "description": "List projects", "tier": "read",
                "input": {"properties": {}, "required": []},
                "backend": {"kind": "http", "method": "GET", "url": "https://site.test/"},
                "extract": {"kind": "css", "fields": {"title": "h1"}},
                "check": {"passed": True, "input": {}, "items_found": 1, "status": 200}}
            async def route(req):
                url = req.request.url
                if "/api/config" in url:
                    await req.fulfill(json={"exploration_available": False})
                elif "/api/sites" in url:
                    await req.fulfill(json={"sites": [{"host": "site.test", "actions": 1, "run_id": "new", "formats": ["manifest.json"]}]})
                elif "/api/history" in url:
                    await req.fulfill(json={"versions": [{"run_id": v, "compiled_at": v, "actions": 1, "formats": ["manifest.json", "misc.md"]} for v in ("new", "old")]})
                elif "/api/manifest" in url:
                    version = "old" if "run_id=old" in url else "new"
                    await req.fulfill(json={"host": "site.test", "run_id": version, "compiled_at": version,
                        "site_description": "A project portfolio.", "actions": [action], "pages": [], "notes": ["Resume is a download."],
                        "procedures": [{"intent": "Read the resume", "steps": [{"note": "Follow the Resume link."}]}]})
                elif "/api/call" in url:
                    calls.append(json.loads(req.request.post_data))
                    await req.fulfill(json={"status": 200, "records": [{"title": "Project"}]})
                else:
                    await req.fulfill(content_type="text/html", body=Path("shim/static/index.html").read_text())
            await page.route("http://shim.test/**", route)
            await page.goto("http://shim.test")
            await page.get_by_role("button", name="site.test (1 tools)").click()
            select = page.get_by_label("Compilation history")
            await select.select_option("old")
            await page.get_by_text("Compilation old", exact=False).wait_for()
            await page.get_by_role("button", name="Call", exact=True).click()
            await page.get_by_text('"Project"', exact=False).wait_for()
            assert calls[0]["run_id"] == "old"
            assert "run_id=old" in await page.get_by_role("link", name="manifest.json", exact=True).get_attribute("href")
            assert await page.get_by_text("Instructions — not independently verified", exact=True).count() == 1
            await page.screenshot(path="/tmp/shim-mvp-desktop.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path="/tmp/shim-mvp-mobile.png", full_page=True)
            await browser.close()
    asyncio.run(run())


def test_empty_intent_checkbox_and_runtime_panel_in_real_browser():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            seen = []
            calls = []
            async def route(req):
                url = req.request.url
                if "/api/config" in url:
                    await req.fulfill(json={"exploration_available": True, "model": "demo-model", "provider": "Anthropic"})
                elif "/api/sites" in url:
                    await req.fulfill(json={"sites": []})
                elif "/api/compile" in url:
                    seen.append(url)
                    events = [{"type": "model_start", "model": "demo-model"},
                              {"type": "model_usage", "model": "demo-model", "input_tokens": 123, "output_tokens": 45, "elapsed_ms": 10},
                              {"type": "exploration_blocked", "method": "POST", "url": "https://site.test/variant", "resource_type": "fetch", "message": "Safety not established."},
                              {"type": "action", "name": "list_projects", "description": "List projects", "tier": "read", "inputs": [],
                               "definition": {"name": "list_projects", "backend": {"kind": "http", "method": "GET", "url": "https://site.test/projects"}, "extract": {"kind": "css", "fields": {"title": "h2"}}}},
                              {"type": "done", "host": "site.test", "shipped": 1, "generated": 1,
                               "pages": 1, "skipped": [], "site_description": "A portfolio of engineering projects.",
                               "notes": ["Resume opens a download."], "formats": ["misc.md", "pages.md"]}]
                    await req.fulfill(content_type="text/event-stream", body="".join(f"data: {json.dumps(e)}\n\n" for e in events))
                elif "/api/call" in url:
                    assert json.loads(req.request.post_data)["arguments"] == {}
                    calls.append(req.request.post_data)
                    await req.fulfill(json={"status": 429, "blocked": True, "records": []} if len(calls) > 1
                                      else {"status": 200, "records": [{"title": "A project"}]})
                else:
                    await req.fulfill(content_type="text/html", body=Path("shim/static/index.html").read_text())
            await page.route("http://shim.test/**", route)
            await page.goto("http://shim.test")
            await page.locator("#url").fill("https://site.test")
            await page.locator("#agent").check()
            await page.locator("#go").click()
            await page.wait_for_function("document.querySelector('#go').disabled === false")
            assert "agent=true" in seen[0]
            assert "demo-model" in await page.locator("#runtime").inner_text()
            assert "123" in await page.locator("#runtime").inner_text()
            assert "https://site.test/variant" in await page.locator("#evidence").text_content()
            assert "Resume opens a download." in await page.locator("#results").inner_text()
            assert "Every candidate" not in await page.locator("#results").inner_text()
            assert await page.get_by_role("link", name="misc.md", exact=True).count() == 1
            assert "A portfolio of engineering projects." in await page.locator("#results").inner_text()
            await page.get_by_text("Request and extractor code", exact=True).click()
            assert "https://site.test/projects" in await page.locator(".tool-code").inner_text()
            await page.get_by_role("button", name="Call", exact=True).click()
            await page.get_by_text('"A project"', exact=False).wait_for()
            await page.get_by_role("button", name="Call", exact=True).click()
            await page.get_by_text("Live site returned HTTP 429", exact=False).wait_for()
            await browser.close()
    asyncio.run(run())


def test_saved_site_selection_is_disabled_during_compilation():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            pending, release = asyncio.Event(), asyncio.Event()
            async def route(req):
                if "/api/config" in req.request.url:
                    await req.fulfill(json={"exploration_available": True, "model": "test", "provider": "test"})
                elif "/api/sites" in req.request.url:
                    await req.fulfill(json={"sites": [{"host": "old.test", "actions": 1, "formats": []}]})
                elif "/api/compile" in req.request.url:
                    pending.set()
                    await release.wait()
                    await req.fulfill(content_type="text/event-stream", body='data: {"type":"error","message":"test ended"}\n\n')
                else:
                    await req.fulfill(content_type="text/html", body=Path("shim/static/index.html").read_text())
            await page.route("http://shim.test/**", route)
            await page.goto("http://shim.test")
            saved = page.get_by_role("button", name="old.test", exact=False)
            await saved.wait_for()
            await page.locator("#go").click()
            await pending.wait()
            disabled = await saved.is_disabled()
            release.set()
            assert disabled
            await page.wait_for_function("document.querySelector('#go').disabled === false")
            await browser.close()
    asyncio.run(run())


def test_dom_inspection_extracts_bounded_real_markup():
    from shim.explore.inspect import inspect_page
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<main><h1>Projects</h1><a href="https://site.test/item">Detail</a><script>secret()</script></main>')
            data = await inspect_page(page, "dom")
            assert "Projects" in data["html"] and "secret()" not in data["html"]
            links = await inspect_page(page, "links")
            assert links["links"] == [{"name": "Detail", "url": "https://site.test/item"}]
            bad = await inspect_page(page, "dom", selector="[")
            assert bad["error"]
            await browser.close()
    asyncio.run(run())
