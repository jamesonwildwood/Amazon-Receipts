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


def test_list_retryable_match_error_order_ids_excludes_apply_errors(temp_db):
    # Match-time error: selected_ynab_txn_id is NULL (never got that far).
    _seed_order("MATCH-ERR", "error")

    # Apply-time error: has a staged candidate/payload, same as a real
    # apply_patch() failure would leave behind.
    _seed_order("APPLY-ERR", "pending_review")
    db.set_match_result("APPLY-ERR", "pending_review", selected_txn_id="txn-1", patch_payload_json="{}")
    db.mark_error("APPLY-ERR", "amount changed since match")

    ids = db.list_retryable_match_error_order_ids(max_retries=5)
    assert ids == ["MATCH-ERR"]


def test_list_retryable_match_error_order_ids_respects_max_retries(temp_db):
    _seed_order("A", "error")
    db.increment_retry_count("A")
    db.increment_retry_count("A")

    assert db.list_retryable_match_error_order_ids(max_retries=5) == ["A"]
    assert db.list_retryable_match_error_order_ids(max_retries=2) == []  # already at the cap
    assert db.get_order("A")["retry_count"] == 2


def test_init_db_migrates_a_pre_retry_count_database(tmp_path, monkeypatch):
    """Simulates the real production DB: created before retry_count existed.
    CREATE TABLE IF NOT EXISTS in SCHEMA alone would never add the column to
    an already-existing table — this proves the explicit migration does,
    without touching any existing row's data."""
    import sqlite3

    db_path = tmp_path / "old.db"
    monkeypatch.setattr(settings, "database_path", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE amazon_orders (
            order_id TEXT PRIMARY KEY,
            order_date DATE,
            html_path TEXT NOT NULL,
            scraped_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            parsed_json TEXT,
            parse_status TEXT NOT NULL DEFAULT 'pending',
            parse_error TEXT,
            parsed_at DATETIME,
            grand_total_cents INTEGER,
            match_status TEXT NOT NULL DEFAULT 'pending_parse',
            candidate_ynab_txn_ids TEXT,
            selected_ynab_txn_id TEXT,
            ynab_patch_payload TEXT,
            matched_at DATETIME,
            approved_at DATETIME,
            ynab_transaction_id_patched TEXT,
            ynab_patched_at DATETIME,
            ynab_patch_error TEXT,
            apply_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT INTO amazon_orders (order_id, html_path) VALUES ('PRE-EXISTING', 'x.html')")
    conn.commit()
    conn.close()

    db.init_db()  # must not raise, must not touch existing rows' other columns

    row = db.get_order("PRE-EXISTING")
    assert row["retry_count"] == 0
    assert row["html_path"] == "x.html"

    # Calling init_db() again (e.g. every app startup) must not error on the
    # now-existing column.
    db.init_db()


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
