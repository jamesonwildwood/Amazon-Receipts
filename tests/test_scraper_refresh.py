"""The receipt refresh pass in app/scraper/wrapper.py, driven with fake page
objects -- never a real browser, Amazon, LLM, or YNAB (the suite's hard
constraint)."""
from decimal import Decimal

import pytest

from app import db
from app.accounts import AmazonAccount
from app.config import settings
from app.models import Item, Receipt
from app.scraper import wrapper


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "receipts_dir", str(tmp_path / "receipts"))
    monkeypatch.setattr(settings, "chrome_profile_dir", str(tmp_path / "chrome"))
    db.init_db()
    yield


def _invoice(total=None, cancelled=False):
    body = "<div>Order placed</div>"
    if cancelled:
        body += "<div>Your order was cancelled. You have not been charged for this order.</div>"
    if total is not None:
        body += f"<div>Grand Total:</div><div>${total}</div>"
    return f"<html><body>{body}</body></html>"


def _seed_no_candidate(order_id, grand_total, receipts_dir):
    html_path = receipts_dir / f"{order_id}.html"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(_invoice(grand_total))
    db.insert_scraped_order(order_id, html_path=str(html_path), amazon_account="jameson")
    receipt = Receipt(
        grand_total=Decimal(grand_total), subtotal=Decimal(grand_total), total_before_tax=Decimal(grand_total),
        date="2026-08-18", items=[Item(price=Decimal(grand_total), title="Chia", short_name="Chia", category="other")],
    )
    db.update_parsed(order_id, receipt)
    db.set_match_result(order_id, "no_candidate")
    return html_path


def test_refresh_outcome_decisions():
    assert wrapper.refresh_outcome(1049, _invoice("10.52")) == Decimal("10.52")  # repriced at shipment
    assert wrapper.refresh_outcome(1049, _invoice("10.49")) == "unchanged"
    assert wrapper.refresh_outcome(1049, _invoice()) == "no_total"  # not shipped yet
    assert wrapper.refresh_outcome(1049, _invoice(cancelled=True)) == "cancelled"
    assert wrapper.refresh_outcome(770, _invoice(cancelled=True)) == "cancelled"  # cancelled wins even with a stale total


def _fake_session(monkeypatch, live_html_by_order):
    """Stands in for build_driver + sign-in + the two vendored page objects."""
    import app.scraper.driver as driver_module

    class FakeDriver:
        def quit(self):
            pass

    class FakeSummaryPage:
        def __init__(self, driver):
            pass

        def load(self):
            pass

        def get_order_ids(self):
            return []

        def maybe_next_page(self):
            return None

    loaded = []

    class FakeOrderPage:
        def __init__(self, driver, order_id):
            self.order_id = order_id

        def load(self):
            loaded.append(self.order_id)

        def __str__(self):
            return live_html_by_order[self.order_id]

    monkeypatch.setattr(driver_module, "build_driver", lambda **kwargs: FakeDriver())
    monkeypatch.setattr(wrapper, "_signin", lambda driver, account: None)
    monkeypatch.setattr(wrapper.amazon_pages, "OrdersSummaryPage", FakeSummaryPage)
    monkeypatch.setattr(wrapper.amazon_pages, "OrderPage", FakeOrderPage)
    return loaded


def test_scrape_refreshes_repriced_receipt_and_requeues_it(temp_db, monkeypatch, tmp_path):
    receipts_dir = tmp_path / "receipts"
    old_path = _seed_no_candidate("CHIA", "10.49", receipts_dir)
    _seed_no_candidate("SAME", "7.17", receipts_dir)
    _seed_no_candidate("CANCELLED", "7.70", receipts_dir)
    loaded = _fake_session(monkeypatch, {
        "CHIA": _invoice("10.52"),
        "SAME": _invoice("7.17"),
        "CANCELLED": _invoice(cancelled=True),
    })
    account = AmazonAccount(label="jameson", email="j@example.com", password="pw")

    new_ids = wrapper.scrape_new_orders(account, refresh_order_ids=["CHIA", "SAME", "CANCELLED"])

    assert new_ids == []  # refresh never masquerades as a new order
    assert sorted(loaded) == ["CANCELLED", "CHIA", "SAME"]

    chia = db.get_order("CHIA")
    assert chia["parse_status"] == "pending" and chia["match_status"] == "pending_parse"
    assert "$10.52" in old_path.read_text()  # live invoice now at the canonical path...
    superseded = list(receipts_dir.glob("CHIA.superseded-*.html"))
    assert len(superseded) == 1 and "$10.49" in superseded[0].read_text()  # ...old one kept as the audit trail

    # Unchanged and cancelled: untouched, so no LLM re-parse is triggered for them.
    assert db.get_order("SAME")["match_status"] == "no_candidate"
    assert db.get_order("CANCELLED")["match_status"] == "no_candidate"
    assert list(receipts_dir.glob("SAME.superseded-*")) == []


def test_refresh_skips_orders_no_longer_no_candidate(temp_db, monkeypatch, tmp_path):
    """The refresh list is built before the scrape; an order matched in
    between (e.g. by a concurrent dashboard action) must not be re-fetched
    or reset."""
    receipts_dir = tmp_path / "receipts"
    _seed_no_candidate("RACED", "10.49", receipts_dir)
    db.set_match_result("RACED", "pending_review", selected_txn_id="txn-1", patch_payload_json="{}")
    loaded = _fake_session(monkeypatch, {"RACED": _invoice("10.52")})
    account = AmazonAccount(label="jameson", email="j@example.com", password="pw")

    wrapper.scrape_new_orders(account, refresh_order_ids=["RACED"])

    assert loaded == []
    assert db.get_order("RACED")["match_status"] == "pending_review"
