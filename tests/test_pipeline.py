from decimal import Decimal

import pytest

from app import db, pipeline
from app.config import settings
from app.models import Item, Receipt


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def _seed_no_candidate(order_id, order_date):
    db.insert_scraped_order(order_id, html_path="unused.html")
    receipt = Receipt(
        grand_total=Decimal("10.00"), subtotal=Decimal("10.00"), total_before_tax=Decimal("10.00"),
        date=order_date, items=[Item(price=Decimal("10.00"), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    db.set_match_result(order_id, "no_candidate")


def test_run_pipeline_refuses_concurrent_call(temp_db, monkeypatch):
    """The lock, not scheduler max_instances, is what stops Run Now from racing
    a cron fire into two concurrent Selenium logins. Never call the real
    scraper/LLM/YNAB here — that's the hard constraint for this test suite."""
    monkeypatch.setattr(pipeline, "scrape_new_orders", lambda: (_ for _ in ()).throw(
        AssertionError("must not scrape while lock test holds the lock")
    ))

    assert pipeline._run_lock.acquire(blocking=False)
    try:
        result = pipeline.run_pipeline()
    finally:
        pipeline._run_lock.release()

    assert result is None  # refused, did nothing


def test_run_pipeline_includes_no_candidate_orders_within_retry_window(temp_db, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(pipeline, "scrape_new_orders", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(settings, "ynab_match_window_days", 5)

    # Computed relative to real date.today() (this machine's clock is set to the
    # session's fictional "current date", but that's still whatever
    # dt.date.today() returns) rather than a hardcoded date, so the test stays
    # correct regardless of when it runs. Window is match_window + pad = 15 days.
    today = dt.date.today()
    recent_date = (today - dt.timedelta(days=3)).isoformat()  # well within 15 days
    old_date = (today - dt.timedelta(days=365)).isoformat()  # far outside it

    _seed_no_candidate("TOO-OLD", old_date)
    _seed_no_candidate("RECENT", recent_date)

    matched_order_ids = []
    monkeypatch.setattr(pipeline, "match_order", lambda order_id, **kwargs: matched_order_ids.append(order_id))

    run_id = pipeline.run_pipeline()

    assert run_id is not None
    assert "RECENT" in matched_order_ids
    assert "TOO-OLD" not in matched_order_ids


def test_run_pipeline_retries_match_errors_but_not_apply_errors(temp_db, monkeypatch):
    monkeypatch.setattr(pipeline, "scrape_new_orders", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    _seed_no_candidate("MATCH-ERR", "2026-01-01")
    db.set_match_result("MATCH-ERR", "error")  # simulates a prior match-time failure

    _seed_no_candidate("APPLY-ERR", "2026-01-01")
    db.set_match_result("APPLY-ERR", "pending_review", selected_txn_id="txn-1", patch_payload_json="{}")
    db.mark_error("APPLY-ERR", "amount changed since match")  # simulates a prior apply-time failure

    matched_order_ids = []
    monkeypatch.setattr(pipeline, "match_order", lambda order_id, **kwargs: matched_order_ids.append(order_id))

    pipeline.run_pipeline()

    assert "MATCH-ERR" in matched_order_ids
    assert "APPLY-ERR" not in matched_order_ids  # terminal -- needs human eyes, not auto-retried


def test_run_pipeline_fetches_transactions_once_for_multiple_orders(temp_db, monkeypatch):
    """The actual rate-limit fix: one YNAB call for the whole run, not one per
    order. A live batch run hit YNAB's 200/hour limit before this existed."""
    monkeypatch.setattr(pipeline, "scrape_new_orders", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")

    import datetime as dt

    recent_date = (dt.date.today() - dt.timedelta(days=3)).isoformat()  # within the retry window
    for i in range(5):
        _seed_no_candidate(f"ORDER-{i}", recent_date)

    from app.ynab import matcher

    calls = []
    monkeypatch.setattr(
        matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: calls.append(since_date) or []
    )

    pipeline.run_pipeline()

    assert len(calls) == 1  # exactly one fetch, regardless of order count
    for i in range(5):
        assert db.get_order(f"ORDER-{i}")["match_status"] == "no_candidate"  # still matched correctly


def test_run_pipeline_records_a_run_row_even_on_scrape_failure(temp_db, monkeypatch):
    def _boom():
        raise RuntimeError("simulated scrape failure")

    monkeypatch.setattr(pipeline, "scrape_new_orders", _boom)

    run_id = pipeline.run_pipeline()
    assert run_id is not None

    run = db.get_last_run()
    assert run["id"] == run_id
    assert run["status"] == "error"
    assert "simulated scrape failure" in run["error_message"]
