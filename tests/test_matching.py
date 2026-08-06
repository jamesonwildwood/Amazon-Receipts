import datetime as dt
from decimal import Decimal

import pytest

from app import db
from app.config import settings
from app.models import Category, Item, Receipt
from app.ynab import matcher


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def _receipt(*prices, grand_total=None):
    items = [
        Item(price=Decimal(p), title=f"Item {i}", short_name=f"Item {i}", category="other")
        for i, p in enumerate(prices)
    ]
    subtotal = sum(Decimal(p) for p in prices)
    gt = Decimal(grand_total) if grand_total is not None else subtotal
    return Receipt(items=items, total_before_tax=subtotal, subtotal=subtotal, grand_total=gt, date="2026-01-05")


def test_build_patch_payload_single_item_no_split():
    receipt = _receipt("10.00")
    payload = matcher.build_patch_payload(receipt, "ORDER-1", {})
    assert "subtransactions" not in payload
    assert payload["memo"] == "Item 0 (ORDER-1)"
    assert payload["category_id"] is None


def test_build_patch_payload_multi_item_splits_and_sums_to_grand_total():
    receipt = _receipt("10.00", "20.00", "5.00", grand_total="36.05")  # tax included
    payload = matcher.build_patch_payload(receipt, "ORDER-2", {})
    subs = payload["subtransactions"]
    assert len(subs) == 3
    assert sum(s["amount"] for s in subs) == -36050  # milliunits, exact match to grand_total
    assert all(s["amount"] < 0 for s in subs)


def test_build_patch_payload_resolves_known_category():
    receipt = _receipt("10.00")
    receipt.items[0].category = "pet supplies"
    categories_map = {
        "pet supplies": Category(group="Household", name="Pet Supplies", category_id="cat-9")
    }
    payload = matcher.build_patch_payload(receipt, "ORDER-3", categories_map)
    assert payload["category_id"] == "cat-9"


def test_find_candidates_filters_by_amount_date_payee_and_category(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")
    monkeypatch.setattr(settings, "ynab_match_window_days", 5)
    monkeypatch.setattr(settings, "ynab_only_match_uncategorized", True)
    monkeypatch.setattr(settings, "ynab_amazon_payee_filters", "Amazon")

    txns = [
        {"id": "t-good", "date": "2026-01-06", "amount": -10000, "category_id": None, "payee_name": "Amazon.com", "deleted": False},
        {"id": "t-wrong-amount", "date": "2026-01-06", "amount": -9999, "category_id": None, "payee_name": "Amazon.com", "deleted": False},
        {"id": "t-categorized", "date": "2026-01-06", "amount": -10000, "category_id": "some-cat", "payee_name": "Amazon.com", "deleted": False},
        {"id": "t-wrong-payee", "date": "2026-01-06", "amount": -10000, "category_id": None, "payee_name": "Costco", "deleted": False},
        {"id": "t-too-late", "date": "2026-01-20", "amount": -10000, "category_id": None, "payee_name": "Amazon.com", "deleted": False},
        {"id": "t-deleted", "date": "2026-01-06", "amount": -10000, "category_id": None, "payee_name": "Amazon.com", "deleted": True},
    ]
    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: txns)

    candidates = matcher.find_candidates(dt.date(2026, 1, 5), 10000)

    assert [c["id"] for c in candidates] == ["t-good"]


def test_match_order_degrades_to_error_on_ynab_api_failure(temp_db, monkeypatch):
    """Regression test: match_order must catch a YNAB API failure (bad token,
    network blip, etc.) and record it on the order, not let it propagate and
    crash the caller — this crashed a live dashboard request with a 500 before
    this guard was added (find_candidates raised requests.HTTPError unguarded)."""
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")

    def _raise(*args, **kwargs):
        raise Exception("401 Client Error: Unauthorized")

    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", _raise)

    order_id = "ERROR-ORDER"
    db.insert_scraped_order(order_id, html_path="unused.html")
    db.update_parsed(order_id, _receipt("10.00"))

    matcher.match_order(order_id)  # must not raise

    row = db.get_order(order_id)
    assert row["match_status"] == "error"
    assert "matching failed" in row["ynab_patch_error"]


def test_find_candidates_excludes_already_bound_transaction(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")
    monkeypatch.setattr(settings, "ynab_amazon_payee_filters", "")

    db.insert_scraped_order("BOUND-ORDER", html_path="unused.html")
    db.update_parsed("BOUND-ORDER", _receipt("10.00"))
    db.set_match_result("BOUND-ORDER", "pending_review", selected_txn_id="t-taken", patch_payload_json="{}")
    db.mark_approved("BOUND-ORDER", "t-taken", "{}")

    txns = [{"id": "t-taken", "date": "2026-01-06", "amount": -10000, "category_id": None, "payee_name": "Amazon", "deleted": False}]
    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: txns)

    candidates = matcher.find_candidates(dt.date(2026, 1, 5), 10000)
    assert candidates == []
