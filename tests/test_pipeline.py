import datetime as dt
from decimal import Decimal

import pytest

from app import db, pipeline
from app.accounts import AmazonAccount
from app.config import settings
from app.models import Item, Receipt


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def _seed_no_candidate(order_id, order_date, amazon_account="default"):
    db.insert_scraped_order(order_id, html_path="unused.html", amazon_account=amazon_account)
    receipt = Receipt(
        grand_total=Decimal("10.00"), subtotal=Decimal("10.00"), total_before_tax=Decimal("10.00"),
        date=order_date, items=[Item(price=Decimal("10.00"), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    db.set_match_result(order_id, "no_candidate")


def _account(label, ynab_account_id=None):
    return AmazonAccount(label=label, email=f"{label}@example.com", password="pw", ynab_account_id=ynab_account_id)


# --- cross-process run lock (docs/IMPROVEMENTS.md 3.7) ---

def test_run_pipeline_refuses_concurrent_call(temp_db, monkeypatch):
    """The file lock, not scheduler max_instances, is what stops Run Now (or a
    CLI run) from racing a cron fire into two concurrent Selenium logins.
    Never call the real scraper/LLM/YNAB here — that's the hard constraint
    for this test suite."""
    monkeypatch.setattr(
        pipeline,
        "scrape_new_orders",
        lambda account, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not scrape while lock test holds the lock")
        ),
    )

    lock_file = pipeline._acquire_lock()
    assert lock_file is not None
    try:
        result = pipeline.run_pipeline()
    finally:
        pipeline._release_lock(lock_file)

    assert result is None  # refused, did nothing


def test_run_pipeline_lock_conflicts_even_within_one_process(temp_db):
    """flock() must be freshly acquired (a new open()) on every call. If two
    callers ever shared one module-level file handle, a second flock() on
    the *same* open file description would silently succeed instead of
    conflicting -- this proves each _acquire_lock() call is independent and
    still correctly conflicts, which is what makes the cross-process
    (tests/test_lock.py) case work too."""
    first = pipeline._acquire_lock()
    assert first is not None
    try:
        second = pipeline._acquire_lock()
        assert second is None
    finally:
        pipeline._release_lock(first)

    # Released -- a fresh acquire now succeeds.
    third = pipeline._acquire_lock()
    assert third is not None
    pipeline._release_lock(third)


# --- multi-account scraping (docs/IMPROVEMENTS.md 3.3) ---

def test_run_pipeline_scrapes_every_configured_account(temp_db, monkeypatch):
    accounts = [_account("jameson"), _account("spouse")]
    monkeypatch.setattr(pipeline, "load_accounts", lambda: accounts)
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    calls = []
    monkeypatch.setattr(
        pipeline, "scrape_new_orders", lambda account, headless=None: calls.append((account.label, headless)) or []
    )

    run_id = pipeline.run_pipeline(headless=True)

    assert run_id is not None
    assert calls == [("jameson", True), ("spouse", True)]  # sequential, and --headful propagated


def test_run_pipeline_isolates_one_account_scrape_failure(temp_db, monkeypatch):
    """A failure scraping one account must not abort the other -- the run is
    marked partial, not error, and the healthy account still gets scraped."""
    accounts = [_account("broken"), _account("healthy")]
    monkeypatch.setattr(pipeline, "load_accounts", lambda: accounts)
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    scraped = []

    def _scrape(account, headless=None):
        if account.label == "broken":
            raise RuntimeError("simulated login failure")
        scraped.append(account.label)
        return []

    monkeypatch.setattr(pipeline, "scrape_new_orders", _scrape)

    run_id = pipeline.run_pipeline()

    assert scraped == ["healthy"]  # the other account still ran
    run = db.get_run(run_id)
    assert run["status"] == "partial"
    assert "broken" in run["error_message"]


def test_run_pipeline_account_filter_scrapes_only_that_label(temp_db, monkeypatch):
    accounts = [_account("jameson"), _account("spouse")]
    monkeypatch.setattr(pipeline, "load_accounts", lambda: accounts)
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    calls = []
    monkeypatch.setattr(
        pipeline, "scrape_new_orders", lambda account, headless=None: calls.append(account.label) or []
    )

    pipeline.run_pipeline(account_label="spouse")

    assert calls == ["spouse"]


def test_run_pipeline_unknown_account_label_marks_run_error(temp_db, monkeypatch):
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [_account("jameson")])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(
        pipeline,
        "scrape_new_orders",
        lambda account, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not scrape when the requested label doesn't exist")
        ),
    )

    run_id = pipeline.run_pipeline(account_label="does-not-exist")

    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "does-not-exist" in run["error_message"]


def test_run_pipeline_records_error_run_on_unexpected_exception(temp_db, monkeypatch):
    """Per-account scrape failures are caught and downgraded to 'partial'
    (see the isolation test above) -- but a genuinely unexpected failure
    outside that loop must still land the run at 'error' with a message,
    exactly as before multi-account support existed."""
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    def _boom():
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(db, "list_pending_parse_order_ids", _boom)

    run_id = pipeline.run_pipeline()
    assert run_id is not None

    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "simulated unexpected failure" in run["error_message"]


def test_run_pipeline_with_no_accounts_configured_skips_scrape_but_still_runs(temp_db, monkeypatch):
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(
        pipeline,
        "scrape_new_orders",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scrape with no accounts configured")),
    )

    run_id = pipeline.run_pipeline()

    run = db.get_run(run_id)
    assert run["status"] == "success"
    assert run["orders_found"] == 0


# --- pre-existing behavior (docs/IMPROVEMENTS.md items 1/5/6), re-verified against the
#     new multi-account pipeline shape ---

def test_run_pipeline_includes_no_candidate_orders_within_retry_window(temp_db, monkeypatch):
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(settings, "ynab_match_window_days", 5)

    from app.ynab import matcher

    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: [])

    # Computed relative to real date.today() rather than a hardcoded date, so
    # the test stays correct regardless of when it runs. Window is
    # match_window + pad = 15 days.
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
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])

    from app.ynab import matcher

    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: [])

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
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")

    recent_date = (dt.date.today() - dt.timedelta(days=3)).isoformat()  # within the retry window
    for i in range(5):
        _seed_no_candidate(f"ORDER-{i}", recent_date)

    from app.ynab import matcher

    calls = []
    monkeypatch.setattr(
        matcher.ynab_client,
        "get_transactions_since",
        lambda account_id, since_date: calls.append((account_id, since_date)) or [],
    )

    pipeline.run_pipeline()

    assert len(calls) == 1  # exactly one fetch, regardless of order count
    assert calls[0][0] == "acct-1"
    for i in range(5):
        assert db.get_order(f"ORDER-{i}")["match_status"] == "no_candidate"  # still matched correctly


def test_run_pipeline_fetches_transactions_per_distinct_ynab_account_not_per_amazon_account(temp_db, monkeypatch):
    """Two Amazon accounts sharing one YNAB account (the common household
    setup) must produce exactly one fetch for that shared account, not two --
    while a third Amazon account with its own ynab_account_id override gets
    its own separate fetch (docs/IMPROVEMENTS.md 3.4)."""
    accounts = [_account("jameson"), _account("spouse"), _account("other-card", ynab_account_id="acct-2")]
    monkeypatch.setattr(pipeline, "load_accounts", lambda: accounts)
    monkeypatch.setattr(pipeline, "get_ynab_categories", lambda: [])
    monkeypatch.setattr(pipeline, "scrape_new_orders", lambda account, headless=None: [])
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")

    recent_date = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    _seed_no_candidate("FROM-JAMESON", recent_date, amazon_account="jameson")
    _seed_no_candidate("FROM-SPOUSE", recent_date, amazon_account="spouse")
    _seed_no_candidate("FROM-OTHER-CARD", recent_date, amazon_account="other-card")

    from app.ynab import matcher

    calls = []
    monkeypatch.setattr(
        matcher.ynab_client,
        "get_transactions_since",
        lambda account_id, since_date: calls.append(account_id) or [],
    )

    pipeline.run_pipeline()

    # jameson+spouse resolve to the same global acct-1 -> one fetch; other-card's
    # override (acct-2) gets its own -- two fetches total, not three.
    assert sorted(calls) == ["acct-1", "acct-2"]
    for order_id in ("FROM-JAMESON", "FROM-SPOUSE", "FROM-OTHER-CARD"):
        assert db.get_order(order_id)["match_status"] == "no_candidate"
