import json
from decimal import Decimal

import pytest

from app import db
from app.config import settings
from app.models import Item, Receipt


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def _seed_order(order_id, match_status, order_date="2026-01-01", grand_total="10.00"):
    db.insert_scraped_order(order_id, html_path="unused.html")
    receipt = Receipt(
        grand_total=Decimal(grand_total),
        subtotal=Decimal(grand_total),
        total_before_tax=Decimal(grand_total),
        date=order_date,
        items=[Item(price=Decimal(grand_total), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    db.set_match_result(order_id, match_status)
    return order_id


def test_list_orders_by_statuses_spans_multiple_statuses(temp_db):
    _seed_order("A", "pending_review")
    _seed_order("B", "ambiguous")
    _seed_order("C", "error")
    _seed_order("D", "no_candidate")  # not requested, must be excluded

    rows = db.list_orders_by_statuses(("pending_review", "ambiguous", "error"))
    ids = {r["order_id"] for r in rows}
    assert ids == {"A", "B", "C"}


def test_list_orders_by_statuses_empty_tuple_returns_empty(temp_db):
    _seed_order("A", "pending_review")
    assert db.list_orders_by_statuses(()) == []


def test_list_parse_error_orders(temp_db):
    db.insert_scraped_order("PARSE-ERR", html_path="unused.html")
    db.mark_parse_error("PARSE-ERR", "LLM returned invalid JSON")
    db.insert_scraped_order("OK", html_path="unused.html")

    rows = db.list_parse_error_orders()
    assert [r["order_id"] for r in rows] == ["PARSE-ERR"]
    assert rows[0]["parse_error"] == "LLM returned invalid JSON"


def test_mark_rejected_succeeds_from_pending_review_and_ambiguous(temp_db):
    _seed_order("A", "pending_review")
    _seed_order("B", "ambiguous")

    assert db.mark_rejected("A") is True
    assert db.mark_rejected("B") is True
    assert db.get_order("A")["match_status"] == "rejected"
    assert db.get_order("B")["match_status"] == "rejected"


def test_mark_rejected_refuses_from_other_statuses(temp_db):
    _seed_order("A", "approved")
    _seed_order("B", "no_candidate")

    assert db.mark_rejected("A") is False
    assert db.mark_rejected("B") is False
    assert db.get_order("A")["match_status"] == "approved"
    assert db.get_order("B")["match_status"] == "no_candidate"


def test_list_no_candidate_order_ids_since_bounds_by_date(temp_db):
    _seed_order("OLD", "no_candidate", order_date="2020-01-01")
    _seed_order("RECENT", "no_candidate", order_date="2026-06-01")
    _seed_order("MATCHED", "pending_review", order_date="2026-06-01")  # wrong status, excluded

    ids = db.list_no_candidate_order_ids_since("2026-01-01")
    assert ids == ["RECENT"]


def test_mark_stale_runs_as_error(temp_db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (started_at, status) VALUES (datetime('now', '-3 hours'), 'running')"
        )
        conn.execute(
            "INSERT INTO pipeline_runs (started_at, status) VALUES (datetime('now', '-10 minutes'), 'running')"
        )
        conn.commit()

    fixed = db.mark_stale_runs_as_error(older_than_hours=2)
    assert fixed == 1

    runs = db.list_runs()
    statuses = sorted(r["status"] for r in runs)
    assert statuses == ["error", "running"]  # only the 3h-old one flipped, the 10m-old one is still legitimately running
