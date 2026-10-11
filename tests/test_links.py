from shim.translate.from_links import link_actions


def test_one_varying_segment_becomes_a_detail_tool():
    links = [f"https://x.test/ebooks/{i}" for i in (1342, 84, 2701, 11, 1661)] + [
        "https://x.test/about/", "https://x.test/help/faq", "https://x.test/ebooks/search/"]
    acts = link_actions(links)
    names = {a.name: a for a in acts}
    assert "get_ebook" in names
    a = names["get_ebook"]
    assert a.backend.url == "https://x.test/ebooks/{id}"
    assert a.input.required == ["id"]
    assert a.input.properties["id"].examples[:2] == ["11", "1342"]
    assert a.tier == "read"


def test_too_few_members_is_not_a_pattern():
    assert link_actions(["https://x.test/p/1", "https://x.test/p/2"]) == []


def test_section_names_are_not_ids():
    links = [f"https://x.test/about/{s}" for s in ("team", "history", "press", "careers", "contact.html")]
    assert link_actions(links) == []


def test_root_level_is_never_a_tool():
    links = [f"https://x.test/{s}" for s in ("1", "2", "3", "4", "5")]
    assert link_actions(links) == []


def test_slug_ids_qualify():
    links = [f"https://x.test/products/{s}" for s in ("apex-knife-set", "pan-28cm", "cutting-board-oak", "kettle-1-7l")]
    acts = link_actions(links)
    assert [a.name for a in acts] == ["get_product"]
    assert acts[0].backend.url.endswith("/products/{slug}")
