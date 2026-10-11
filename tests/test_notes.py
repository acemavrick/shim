import asyncio
import json

from shim.emit import emit_all
from shim.explore.agent import AgentExplorer, AgentResult
from shim.manifest import Manifest


def test_unreplayable_flow_is_published_as_notes_not_a_tool(tmp_path):
    async def complete(system, prompt):
        return json.dumps({"tools": [], "site_description": "A portfolio of engineering projects.", "notes": ["The resume link starts a download; use the browser's download handler."],
                           "summary": "Read-only portfolio."})
    result = AgentResult()
    asyncio.run(AgentExplorer(complete, "Learn site")._write_down(result))
    assert not result.tools
    assert result.site_description == "A portfolio of engineering projects."
    assert result.notes == ["The resume link starts a download; use the browser's download handler."]
    manifest = Manifest(site="https://site.test", host="site.test", compiled_at="now", notes=result.notes)
    emit_all(manifest, tmp_path)
    assert "download handler" in (tmp_path / "misc.md").read_text()
    assert "download handler" in (tmp_path / "llms.txt").read_text()


def test_exploration_summary_is_available_in_saved_briefing(tmp_path):
    manifest = Manifest(site="https://site.test", host="site.test", compiled_at="now",
                        exploration_summary="Found articles; opened two detail pages; download is browser-only.")
    emit_all(manifest, tmp_path)
    assert "opened two detail pages" in (tmp_path / "misc.md").read_text()
    assert "opened two detail pages" in (tmp_path / "llms.txt").read_text()
