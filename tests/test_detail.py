from shim.runtime.extract import run_extract
from shim.translate.infer import detail_extract, infer_listing

PAGE = """<html><head><title>Blue Top — Shop</title><meta name="description" content="A blue top.">
</head><body><div class="nav"><ul><li><a href="/">Home</a></li><li><a href="/p">Products</a></li>
<li><a href="/c">Cart</a></li><li><a href="/x">Contact</a></li></ul></div>
<div class="product-information"><h2>Blue Top</h2><span class="price">Rs. 500</span></div></body></html>"""


def test_detail_page_is_one_record():
    spec = detail_extract(PAGE)
    rec = run_extract(PAGE, spec).records[0]
    assert rec["title"] == "Blue Top — Shop" and rec["heading"] == "Blue Top"
    assert rec["price"] == 500 and rec["description"] == "A blue top."


def test_classed_nav_is_not_a_listing():
    spec = infer_listing(PAGE)
    assert spec.items is None or "nav" not in spec.items
