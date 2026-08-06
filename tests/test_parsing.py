from decimal import Decimal
from pathlib import Path

from app.models import Category, Item, Receipt, resolve_item_category
from app.parsing.receipt_parser import receipt_html_to_text

FIXTURE = Path(__file__).parent / "fixtures" / "sample_receipt.html"


def test_receipt_html_to_text_strips_tags_and_keeps_content():
    text = receipt_html_to_text(FIXTURE.read_text())
    assert "AmazonBasics 6-Foot HDMI Cable, 2-Pack" in text
    assert "Purina ONE Dry Dog Food, Chicken & Rice, 8 lb Bag" in text
    assert "Grand Total: $50.53" in text
    assert "<table" not in text


def test_receipt_model_validates_llm_shaped_output():
    # Shape an LLM extraction of tests/fixtures/sample_receipt.html would plausibly produce.
    raw = {
        "grand_total": "50.53",
        "subtotal": "47.44",
        "total_before_tax": "47.44",
        "date": "2026-03-03",
        "items": [
            {
                "short_name": "HDMI Cable 2-Pack",
                "title": "AmazonBasics 6-Foot HDMI Cable, 2-Pack",
                "price": "12.99",
                "category": "electronics",
            },
            {
                "short_name": "Dog Food",
                "title": "Purina ONE Dry Dog Food, Chicken & Rice, 8 lb Bag",
                "price": "24.98",
                "category": "pet supplies",
            },
            {
                "short_name": "Sharpie Markers",
                "title": "Sharpie Permanent Markers, Fine Point, Black, 12-Count",
                "price": "9.47",
                "category": "office supplies",
            },
        ],
    }
    receipt = Receipt.model_validate(raw)
    assert receipt.grand_total == Decimal("50.53")
    assert receipt.item_subtotal() == Decimal("47.44")
    assert len(receipt.items) == 3


def test_adjusted_cost_prorates_tax_across_items():
    receipt = Receipt(
        grand_total=Decimal("50.53"),
        subtotal=Decimal("47.44"),
        total_before_tax=Decimal("47.44"),
        date="2026-03-03",
        items=[
            Item(price=Decimal("12.99"), title="Cable", short_name="Cable", category="electronics"),
            Item(price=Decimal("24.98"), title="Dog Food", short_name="Dog Food", category="pet"),
            Item(price=Decimal("9.47"), title="Markers", short_name="Markers", category="office"),
        ],
    )
    adjusted = [item.adjusted_cost(receipt) for item in receipt.items]
    # Proration should preserve the grand total (allowing for cent rounding).
    assert sum(adjusted) == Decimal("50.54") or sum(adjusted) == Decimal("50.53")
    assert all(a > i.price for a, i in zip(adjusted, receipt.items))


def test_resolve_item_category_matches_and_fixes_upstream_bug():
    categories = {
        "pet supplies": Category(group="Household", name="🐶 Pet Supplies", category_id="cat-1"),
    }
    matched = resolve_item_category("Pet Supplies", categories)
    assert matched is not None
    assert matched.category_id == "cat-1"


def test_resolve_item_category_returns_none_when_unmatched():
    categories = {
        "pet supplies": Category(group="Household", name="Pet Supplies", category_id="cat-1"),
    }
    assert resolve_item_category("office supplies", categories) is None
