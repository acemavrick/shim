import asyncio
import json
import pytest
from pathlib import Path

from playwright.async_api import async_playwright


@pytest.mark.parametrize("versioned", [False, True])
def test_candidates_stream_into_verified_callable_tools_before_compile_finishes(versioned):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            calls = []
            definition = {"name": "list_projects", "description": "List projects", "tier": "read",
                          "input": {"properties": {"limit": {"type": "integer", "examples": [3]}}, "required": ["limit"]},
                          "check": {"passed": True, "items_found": 2},
                          "backend": {"kind": "http", "method": "GET", "url": "https://site.test/projects"},
                          "extract": {"kind": "css", "fields": {"title": "h2"}}}
            manifest = {"host": "site.test", "site_description": "Example projects", "actions": [definition], "pages": []}

            async def route(req):
                url = req.request.url
                if "/api/config" in url:
                    await req.fulfill(json={"exploration_available": False})
                elif "/api/sites" in url:
                    await req.fulfill(json={"sites": [{"host": "site.test", "actions": 1, "formats": ["manifest.json"]}]})
                elif "/api/manifest" in url:
                    await req.fulfill(json=manifest)
                elif "/api/call" in url:
                    calls.append(json.loads(req.request.post_data))
                    await req.fulfill(json={"status": 200, "records": [{"title": "Project"}]})
                else:
                    await req.fulfill(content_type="text/html", body=Path("shim/static/index.html").read_text())

            # Control individual stream messages so each intermediate state is
            # observed in Chromium while compilation remains active.
            await page.add_init_script("""
                window.EventSource = class {
                    constructor(url) { this.url = url; window.compileStream = this; }
                    close() { this.closed = true; }
                };
                window.emitCompileEvent = event => window.compileStream.onmessage({data:JSON.stringify(event)});
            """)
            await page.route("http://shim.test/**", route)
            await page.goto("http://shim.test")
            await page.locator("#url").fill("https://site.test")
            await page.locator("#go").click()

            async def emit(event):
                ids = {"run_id": "saved-version", "live_id": "live-registry"} if versioned else {"run_id": "current-run"}
                await page.evaluate("event => window.emitCompileEvent(event)", {**ids, **event})

            await emit({"type": "stage", "stage": "explore", "active_stages": ["explore", "check"]})
            assert "Crawling the site" in await page.locator("#stage-text").text_content()
            assert "Verifying tools" in await page.locator("#stage-text").text_content()
            await emit({"type": "stage", "stage": "agent", "step_budget": 16,
                        "active_stages": ["agent", "explore", "check"]})
            assert "Up to 16 steps" in await page.locator("#exploration-progress").inner_text()
            await emit({"type": "stage", "stage": "translate", "forms": 2,
                        "active_stages": ["agent", "translate"]})
            assert "Reading 2 form(s)" in await page.locator("#stage-text").text_content()
            await emit({"type": "agent_step", "step": 3, "action": "goto", "name": "https://site.test/projects"})
            await emit({"type": "model_start", "model": "test-model"})
            await emit({"type": "stage_end", "stage": "translate", "active_stages": ["agent"]})
            assert await page.locator("#stage-text").text_content() == "Agent exploring"
            assert "Waiting for model response" in await page.locator("#activity").inner_text()
            assert "Last completed step 3 / 16" in await page.locator("#exploration-progress").inner_text()
            assert "1 model response" in await page.locator("#model-wait").inner_text()
            # A concurrent model call is tracked without claiming it is an
            # additional exploration step.
            await emit({"type": "model_start", "model": "test-model"})
            assert "2 model responses" in await page.locator("#model-wait").inner_text()
            await emit({"type": "model_usage", "input_tokens": 10, "output_tokens": 5, "elapsed_ms": 100})
            assert "1 model response" in await page.locator("#model-wait").inner_text()
            assert "Last completed step 3 / 16" in await page.locator("#exploration-progress").inner_text()
            await emit({"type": "model_error", "error": "request failed"})
            assert await page.locator("#model-wait").is_hidden()
            await emit({"type": "stage_end", "stage": "agent", "active_stages": []})
            assert await page.locator("#stage-text").text_content() == "Finishing compilation"
            assert "Exploration finished" in await page.locator("#exploration-progress").inner_text()
            # Older streams without an explicit active-stage list still remove
            # the completed stage rather than leaving it on screen.
            await emit({"type": "stage", "stage": "translate", "forms": 1})
            await emit({"type": "stage_end", "stage": "translate"})
            assert "Reading" not in await page.locator("#stage-text").text_content()
            await emit({"type": "tool_candidate", "candidate_id": "c1", "name": "original_name", "description": "Found a project list",
                        "tier": "read", "schema": definition["input"], "definition": definition})
            card = page.locator('[data-candidate-id="c1"]')
            assert await card.count() == 1
            assert "Discovered" in await card.inner_text()
            assert await card.get_by_role("button", name="Call", exact=True).count() == 0
            assert "Python replay" not in await card.inner_text()
            assert await page.locator("#go").is_disabled()

            await emit({"type": "tool_checking", "candidate_id": "c1"})
            assert await card.get_attribute("data-tool-state") == "checking"
            assert "1 tool verifying" in await page.locator("#stage-text").text_content()
            assert await page.get_by_role("button", name="Call", exact=True).count() == 0

            await emit({"type": "tool_candidate", "candidate_id": "c2", "name": "bad_tool", "description": "Candidate that fails", "tier": "read"})
            await emit({"type": "action", "candidate_id": "c1", "name": "list_projects", "description": "List projects", "tier": "read", "items_found": 2})
            assert await card.count() == 1
            assert await card.get_attribute("data-tool-state") == "verified"
            assert "list_projects(limit)" in await card.inner_text()
            assert "original_name" not in await card.inner_text()
            assert "2 records verified" in await card.inner_text()
            await card.get_by_role("button", name="Call", exact=True).click()
            await card.get_by_text('"Project"', exact=False).wait_for()
            expected = {"host": "site.test", "action": "list_projects", "run_id": "saved-version" if versioned else "current-run",
                        "candidate_id": "c1", "arguments": {"limit": 3}}
            if versioned:
                expected["live_id"] = "live-registry"
            assert calls == [expected]
            assert await page.locator("#go").is_disabled()

            await emit({"type": "action_failed", "candidate_id": "c2", "error": "No records extracted"})
            failed = page.locator('[data-candidate-id="c2"]')
            assert "Verification failed" in await failed.inner_text()
            assert "No records extracted" in await failed.inner_text()
            assert await failed.get_by_role("button", name="Call", exact=True).count() == 0
            assert "2 discovered" in await page.locator(".tool-progress").inner_text()
            assert "1 verified · 1 failed" in await page.locator(".tool-progress").inner_text()

            # Late in-flight messages cannot demote a completed tool.
            await emit({"type": "tool_checking", "candidate_id": "c1"})
            assert await card.get_attribute("data-tool-state") == "verified"
            await emit({"type": "done", "host": "site.test", "shipped": 1, "generated": 2, "pages": 1,
                        "skipped": [], "formats": ["manifest.json"]})
            assert not await page.locator("#go").is_disabled()
            assert await page.get_by_role("link", name="manifest.json", exact=True).count() == 1
            await card.get_by_role("button", name="Call", exact=True).click()
            await card.get_by_text('"Project"', exact=False).wait_for()
            expected_saved = {"host": "site.test", "action": "list_projects", "arguments": {"limit": 3}}
            if versioned:
                expected_saved["run_id"] = "saved-version"
            assert calls[-1] == expected_saved

            # Saved compilations keep using the saved action, with no live run id.
            saved = page.get_by_role("button", name="site.test (1 tools)", exact=True)
            await saved.click()
            await page.get_by_text("Saved: site.test", exact=True).wait_for()
            await page.get_by_role("button", name="Call", exact=True).click()
            await page.get_by_text('"Project"', exact=False).wait_for()
            assert calls[-1] == {"host": "site.test", "action": "list_projects", "arguments": {"limit": 3}}
            await browser.close()

    asyncio.run(run())
