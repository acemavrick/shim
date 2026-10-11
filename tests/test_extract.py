from pathlib import Path

from shim.manifest import Extract
from shim.runtime.extract import parse_field_spec, run_extract, to_number

HTML = Path("fixtures/search-form.html").read_text()


def test_parse_field_spec_splits_attr_and_cast():
    assert parse_field_spec(".price|number") == type(parse_field_spec(".price|number"))(
        selector=".price", attr=None, cast="number"
    )
    f = parse_field_spec("a@href")
    assert (f.selector, f.attr, f.cast) == ("a", "href", None)
    f = parse_field_spec("@data-id")
    assert (f.selector, f.attr, f.cast) == ("", "data-id", None)


def test_to_number_tolerates_currency():
    assert to_number("$89.00") == 89
    assert to_number("$1,249.50") == 1249.5
    assert to_number("no digits") is None


def test_extracts_records_with_attrs_numbers_and_resolved_urls():
    result = run_extract(
        HTML,
        Extract(kind="css", items=".product-card", fields={
            "id": "@data-id", "name": ".title", "price": ".price|number",
            "url": ".title@href", "missing": ".nope",
        }),
        "https://shop.example.com/catalog/",
    )
    assert result.items_found == 2
    assert result.records[0] == {
        "id": "tr2", "name": "Trail Runner 2", "price": 89,
        "url": "https://shop.example.com/products/trail-runner-2", "missing": None,
    }
    assert result.records[1]["price"] == 1249.5


def test_no_items_selector_yields_single_record():
    result = run_extract(HTML, Extract(kind="css", fields={"firstPrice": ".price|number"}))
    assert result.items_found == 1
    assert result.records[0] == {"firstPrice": 89}


def test_json_extraction_walks_dot_paths():
    body = '{"results":[{"id":"a","meta":{"title":"Alpha"},"cost":"12.50"}]}'
    result = run_extract(body, Extract(kind="json", items="results", fields={
        "id": "id", "name": "meta.title", "cost": "cost|number",
    }))
    assert result.records[0] == {"id": "a", "name": "Alpha", "cost": 12.5}


def test_malformed_json_yields_no_records():
    result = run_extract("<html>not json</html>", Extract(kind="json", fields={"a": "a"}))
    assert result.items_found == 0
