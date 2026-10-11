from shim.manifest import Manifest, Page
from shim.emit import agent_page, emit_all, llms_txt


def test_content_only_site_publishes_readable_pages(tmp_path):
    manifest = Manifest(site="https://portfolio.test", host="portfolio.test", compiled_at="2026-10-10T00:00:00Z",
                        pages=[Page(url="https://portfolio.test/about", title="About", markdown="# About\n\nI build compilers.")])
    emit_all(manifest, tmp_path)
    briefing = llms_txt(manifest)
    assert 'I build compilers.' in briefing
    assert '[About](https://portfolio.test/about)' in briefing
    assert (tmp_path / 'pages.md').read_text().count('I build compilers.') == 1
    assert 'I build compilers.' in agent_page(manifest)


def test_page_content_removes_navigation_and_resolves_links():
    from shim.translate.content import page_markdown
    html = '<html><nav>Menu clutter</nav><main><!-- layout notes --><h1>About</h1><p>I build compilers. <a href="/projects">Projects</a></p><script>bad()</script></main></html>'
    result = page_markdown(html, 'https://portfolio.test/about')
    assert '# About' in result and 'I build compilers.' in result
    assert '(https://portfolio.test/projects)' in result
    assert 'Menu clutter' not in result and 'bad()' not in result
    assert 'layout notes' not in result
