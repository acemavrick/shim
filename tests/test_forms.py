from pathlib import Path

from shim.translate.forms import parse_forms

HTML = Path("fixtures/search-form.html").read_text()
FORMS = parse_forms(HTML, "https://shop.example.com/catalog/page-1.html")


def _by_wire(form):
    return {c.wire: c for c in form.controls}


def test_resolves_action_against_base_href():
    assert FORMS[0].action == "https://shop.example.com/search?page=2"
    assert FORMS[0].method == "GET"


def test_excludes_disabled_nameless_reset_and_file_controls():
    assert sorted(c.wire for c in FORMS[0].controls) == [
        "category", "in_stock", "q", "size", "sort",
    ]
    assert FORMS[2].controls == [], "file input must not become a parameter"


def test_captures_markup_defaults_that_must_be_pinned():
    by = _by_wire(FORMS[0])
    assert by["category"].kind == "hidden"
    assert by["category"].value == "shoes"
    assert by["sort"].value == "price_asc", "selected option is the default"
    assert by["in_stock"].value == "1", "checked checkbox contributes its value"
    assert by["size"].value == "m", "checked radio wins"


def test_enumerates_select_and_radio_options():
    by = _by_wire(FORMS[0])
    assert by["sort"].options == ["rel", "price_asc"]
    assert by["size"].options == ["m", "l"]


def test_carries_label_text_and_constraints():
    q = _by_wire(FORMS[0])["q"]
    assert q.label == "Search catalog"
    assert q.required is True
    assert q.max_length == 80


def test_records_submit_button_branching():
    assert [(s.wire, s.value, s.text) for s in FORMS[1].submits] == [
        ("intent", "add_to_cart", "Add to cart"),
        ("intent", "buy_now", "Buy now"),
    ]


def test_signature_is_stable_across_pages():
    other = parse_forms(HTML, "https://shop.example.com/catalog/page-2.html")
    assert FORMS[0].signature == other[0].signature


def test_flags_multipart_enctype():
    assert FORMS[2].enctype == "multipart/form-data"
