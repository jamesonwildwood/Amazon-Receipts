import json
from decimal import Decimal

import pytest

from app import db
from app.config import settings
from app.models import Item, Receipt
from app.ynab import apply as apply_module
from app.ynab import matcher


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def _seed_pending_review_order(order_id="TEST-1", txn_id="txn-abc"):
    db.insert_scraped_order(order_id, html_path="unused.html")
    receipt = Receipt(
        grand_total=Decimal("10.00"),
        subtotal=Decimal("10.00"),
        total_before_tax=Decimal("10.00"),
        date="2026-01-01",
        items=[Item(price=Decimal("10.00"), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    payload = json.dumps({"memo": f"Widget ({order_id})", "category_id": None})
    db.set_match_result(order_id, "pending_review", selected_txn_id=txn_id, patch_payload_json=payload)
    return order_id, txn_id


def test_apply_patch_success(temp_db, monkeypatch):
    order_id, txn_id = _seed_pending_review_order()
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    calls = []
    monkeypatch.setattr(
        apply_module.ynab_client,
        "patch_transaction",
        lambda tid, payload: calls.append((tid, payload)) or {"id": tid},
    )

    result = apply_module.apply_patch(order_id)

    assert result.ok
    assert result.reason == "applied"
    row = db.get_order(order_id)
    assert row["match_status"] == "approved"
    assert row["ynab_transaction_id_patched"] == txn_id
    assert row["apply_count"] == 1
    assert len(calls) == 1


def test_apply_patch_refuses_when_amount_changed_since_match(temp_db, monkeypatch):
    order_id, txn_id = _seed_pending_review_order()  # seeded for a $10.00 order -> expects -10000 milliunits
    monkeypatch.setattr(
        apply_module.ynab_client,
        "get_transaction",
        lambda tid: {"id": tid, "deleted": False, "amount": -12340, "cleared": "uncleared"},  # edited since match
    )
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: calls.append(tid))

    result = apply_module.apply_patch(order_id)

    assert not result.ok
    assert result.reason == "amount_changed"
    assert len(calls) == 0  # never touched YNAB
    assert db.get_order(order_id)["match_status"] == "error"


def test_apply_patch_refuses_when_transaction_reconciled(temp_db, monkeypatch):
    order_id, txn_id = _seed_pending_review_order()
    monkeypatch.setattr(
        apply_module.ynab_client,
        "get_transaction",
        lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "reconciled"},
    )
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: calls.append(tid))

    result = apply_module.apply_patch(order_id)

    assert not result.ok
    assert result.reason == "transaction_reconciled"
    assert len(calls) == 0


def test_apply_patch_double_approve_is_noop(temp_db, monkeypatch):
    order_id, txn_id = _seed_pending_review_order()
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    calls = []
    monkeypatch.setattr(
        apply_module.ynab_client,
        "patch_transaction",
        lambda tid, payload: calls.append((tid, payload)) or {"id": tid},
    )

    first = apply_module.apply_patch(order_id)
    second = apply_module.apply_patch(order_id)

    assert first.ok and first.reason == "applied"
    assert second.ok and second.reason == "already_applied"
    assert len(calls) == 1  # exactly one PATCH ever sent, despite two Approve calls
    row = db.get_order(order_id)
    assert row["apply_count"] == 1


def test_apply_patch_refuses_concurrent_claim(temp_db):
    order_id, _ = _seed_pending_review_order()
    # Simulate an in-flight claim (e.g. an overlapping scheduler run) by moving
    # the row into 'applying' before calling apply_patch.
    db.claim_for_apply(order_id, ("pending_review",))

    result = apply_module.apply_patch(order_id)

    assert not result.ok
    assert result.reason == "already_processing"


def test_apply_patch_refuses_transaction_already_claimed_by_another_order(temp_db, monkeypatch):
    order_a, txn_id = _seed_pending_review_order(order_id="TEST-A", txn_id="txn-shared")
    order_b, _ = _seed_pending_review_order(order_id="TEST-B", txn_id="txn-shared")
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: {"id": tid})

    first = apply_module.apply_patch(order_a)
    second = apply_module.apply_patch(order_b)

    assert first.ok
    assert not second.ok
    assert second.reason == "transaction_already_claimed"


def test_reapply_disabled_by_default(temp_db, monkeypatch):
    order_id, _ = _seed_pending_review_order()
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: {"id": tid})
    apply_module.apply_patch(order_id)

    result = apply_module.apply_patch(order_id, allow_reapply=True)

    assert not result.ok
    assert result.reason == "reapply_disabled"


def test_reapply_overwrites_same_transaction_when_enabled(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "allow_reapply", True)
    order_id, txn_id = _seed_pending_review_order()
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    calls = []
    monkeypatch.setattr(
        apply_module.ynab_client, "patch_transaction", lambda tid, payload: calls.append(tid) or {"id": tid}
    )

    apply_module.apply_patch(order_id)
    result = apply_module.apply_patch(order_id, allow_reapply=True)

    assert result.ok
    assert result.reason == "applied"
    assert calls == [txn_id, txn_id]  # same transaction both times, never a different one
    row = db.get_order(order_id)
    assert row["apply_count"] == 2
    log = db.get_apply_log(order_id)
    assert len(log) == 2
    assert [bool(entry["is_reapply"]) for entry in log] == [True, False]  # most recent first


def _seed_no_candidate_order(order_id="TEST-NC", grand_total="12.34"):
    db.insert_scraped_order(order_id, html_path="unused.html")
    receipt = Receipt(
        grand_total=Decimal(grand_total),
        subtotal=Decimal(grand_total),
        total_before_tax=Decimal(grand_total),
        date="2026-01-01",
        items=[Item(price=Decimal(grand_total), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    db.set_match_result(order_id, "no_candidate")
    return order_id


def test_create_transaction_disabled_by_default(temp_db, monkeypatch):
    order_id = _seed_no_candidate_order()
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "post_transaction", lambda payload: calls.append(payload))

    result = apply_module.create_transaction(order_id)

    assert not result.ok
    assert result.reason == "create_without_match_disabled"
    assert len(calls) == 0  # never touched YNAB
    assert db.get_order(order_id)["match_status"] == "no_candidate"  # untouched


def test_create_transaction_success(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_allow_create_without_match", True)
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")
    order_id = _seed_no_candidate_order()
    calls = []
    monkeypatch.setattr(
        apply_module.ynab_client,
        "post_transaction",
        lambda payload: calls.append(payload) or {"id": "new-txn-1"},
    )

    result = apply_module.create_transaction(order_id)

    assert result.ok
    assert result.reason == "created"
    assert result.ynab_transaction_id == "new-txn-1"
    assert len(calls) == 1
    payload = calls[0]
    assert payload["account_id"] == "acct-1"
    assert payload["date"] == "2026-01-01"
    assert payload["amount"] == -12340
    assert payload["payee_name"] == "Amazon"

    row = db.get_order(order_id)
    assert row["match_status"] == "approved"
    assert row["ynab_transaction_id_patched"] == "new-txn-1"
    assert row["apply_count"] == 1


def test_create_transaction_double_call_is_noop(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_allow_create_without_match", True)
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")
    order_id = _seed_no_candidate_order()
    calls = []
    monkeypatch.setattr(
        apply_module.ynab_client, "post_transaction", lambda payload: calls.append(payload) or {"id": "new-txn-1"}
    )

    first = apply_module.create_transaction(order_id)
    second = apply_module.create_transaction(order_id)

    assert first.ok and first.reason == "created"
    assert second.ok and second.reason == "already_applied"
    assert len(calls) == 1  # exactly one POST ever sent, despite two calls


def test_create_transaction_refuses_when_not_no_candidate(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_allow_create_without_match", True)
    order_id, _ = _seed_pending_review_order()  # match_status == 'pending_review', not 'no_candidate'
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "post_transaction", lambda payload: calls.append(payload))

    result = apply_module.create_transaction(order_id)

    assert not result.ok
    assert len(calls) == 0  # never touched YNAB


def test_reset_order_requires_allow_reset_flag(temp_db, monkeypatch):
    order_id, txn_id = _seed_pending_review_order()
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: {"id": tid})
    apply_module.apply_patch(order_id)

    # Reset re-runs the matcher, which calls out to YNAB for candidate transactions —
    # stub that so this test stays offline; returning none is fine, we're testing the
    # ALLOW_RESET gate and the apply-history preservation, not matching itself.
    monkeypatch.setattr(matcher.ynab_client, "get_transactions_since", lambda account_id, since_date: [])

    assert apply_module.reset_order(order_id, "pending_review") is False  # ALLOW_RESET is false by default

    monkeypatch.setattr(settings, "allow_reset", True)
    assert apply_module.reset_order(order_id, "pending_review") is True

    row = db.get_order(order_id)
    # Resetting/re-matching must never clear apply history from the prior real apply.
    assert row["ynab_transaction_id_patched"] == txn_id
    assert row["apply_count"] == 1
    assert len(db.get_apply_log(order_id)) == 1
    assert row["match_status"] == "no_candidate"  # matcher ran fresh, found nothing (stubbed empty)
