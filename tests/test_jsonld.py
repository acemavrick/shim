from pathlib import Path

from shim.translate.jsonld import dedupe_records, jsonld_records

RECORDS = jsonld_records(Path("fixtures/jsonld.html").read_text())
BY_TYPE = {}
for r in RECORDS:
    BY_TYPE.setdefault(r["type"], []).append(r)


def test_product_is_flattened_with_offer_and_rating():
    p = BY_TYPE["Product"][0]
    assert p["name"] == "Trail Runner 2"
    assert p["sku"] == "tr2"
    assert p["price"] == 89 and p["priceCurrency"] == "USD"
    assert p["availability"] == "InStock", "schema.org URL enum collapsed to its name"
    assert p["brand_name"] == "Example"
    assert p["aggregateRating_ratingValue"] == 4.6
    assert p["description"] == "A light trail shoe.", "html stripped"
    assert p["image"] == ["https://shop.example.com/i/tr2.jpg"]


def test_graph_is_unnested_and_chrome_types_dropped():
    assert "Organization" in BY_TYPE and "WebSite" in BY_TYPE
    assert "BreadcrumbList" not in BY_TYPE
    assert BY_TYPE["Organization"][0]["sameAs"] == ["https://instagram.com/example"]


def test_trailing_comma_is_repaired():
    names = [r["name"] for r in BY_TYPE["Product"]]
    assert "Broken, trailing comma" in names


def test_repeated_site_block_dedupes_to_one():
    org = {"type": "Organization", "name": "Example Shop", "url": "https://shop.example.com"}
    assert len(dedupe_records([org, dict(org), dict(org)])) == 1
    a = {"type": "Product", "name": "A", "sku": "1"}; b = {"type": "Product", "name": "A", "sku": "2"}
    assert len(dedupe_records([a, b])) == 2, "different skus are different products"
