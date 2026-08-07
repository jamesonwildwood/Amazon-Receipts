import datetime as dt
import json
import logging

from app import db
from app.config import settings
from app.models import Receipt, resolve_item_category
from app.parsing.categories import categories_by_name, get_ynab_categories
from app.ynab import client as ynab_client

logger = logging.getLogger(__name__)


def _milliunits(amount) -> int:
    return int(round(float(amount) * 1000))


def load_categories_map() -> dict:
    if not settings.ynab_personal_access_token:
        return {}
    try:
        return categories_by_name(get_ynab_categories())
    except Exception:
        # Category lookup is an enhancement (auto-categorization), not a hard
        # requirement for a match/payload to exist — degrade to no categories
        # rather than fail the whole match on a transient YNAB API error.
        logger.exception("Failed to fetch YNAB categories; proceeding without category resolution")
        return {}


def find_candidates(order_date: dt.date, grand_total_milliunits: int) -> list[dict]:
    """Candidate-finding per docs/DESIGN.md §5: a window around the order date,
    exact amount match, uncategorized-only and payee filters (both configurable),
    excluding any transaction already bound to a different approved order."""
    window_start = order_date - dt.timedelta(days=settings.ynab_match_window_days)
    window_end = order_date + dt.timedelta(days=settings.ynab_match_window_days)

    transactions = ynab_client.get_transactions_since(settings.ynab_account_id, window_start.isoformat())
    already_bound = db.bound_transaction_ids()
    payee_filters = settings.ynab_amazon_payee_filter_list

    candidates = []
    for txn in transactions:
        if txn.get("deleted"):
            continue
        if dt.date.fromisoformat(txn["date"]) > window_end:
            continue
        if abs(txn["amount"]) != abs(grand_total_milliunits):
            continue
        if settings.ynab_only_match_uncategorized and txn.get("category_id") is not None:
            continue
        payee = txn.get("payee_name") or ""
        if payee_filters and not any(f.lower() in payee.lower() for f in payee_filters):
            continue
        if txn["id"] in already_bound:
            continue
        candidates.append(txn)
    return candidates


def build_patch_payload(receipt: Receipt, order_id: str, categories_map: dict, sign: int = -1) -> dict:
    """Full-replacement PATCH body — always the complete desired state, never a
    partial/append, so re-applying overwrites instead of stacking (docs/DESIGN.md §5).

    sign: -1 for the normal case (an outflow/charge — the default). Pass +1 when
    enriching a refund/credit transaction for the same order (same items/categories,
    but subtransaction amounts must match the parent transaction's own sign, or
    YNAB will reject them / they won't sum correctly)."""
    if len(receipt.items) == 1:
        item = receipt.items[0]
        category = resolve_item_category(item.category, categories_map)
        return {
            "memo": f"{item.short_name} ({order_id})",
            "category_id": category.category_id if category else None,
        }

    grand_total_milliunits = sign * abs(_milliunits(receipt.grand_total))
    subtransactions = []
    running_total = 0
    for i, item in enumerate(receipt.items):
        category = resolve_item_category(item.category, categories_map)
        if i == len(receipt.items) - 1:
            # last item absorbs cent-rounding drift so subtransactions sum exactly
            amount = grand_total_milliunits - running_total
        else:
            amount = sign * abs(_milliunits(item.adjusted_cost(receipt)))
        running_total += amount
        subtransactions.append(
            {
                "amount": amount,
                "memo": item.title,
                "category_id": category.category_id if category else None,
            }
        )
    return {
        "memo": f"Amazon order {order_id}",
        "subtransactions": subtransactions,
    }


def build_create_payload(receipt: Receipt, order_id: str, categories_map: dict) -> dict:
    """Full payload for creating a brand-new transaction — used only for the
    no-bank-match backfill path (app/ynab/apply.py:create_transaction), when
    match_status == 'no_candidate'. Unlike build_patch_payload, this must
    include account_id/date/amount/payee_name since there's no existing
    transaction to inherit them from."""
    payload = build_patch_payload(receipt, order_id, categories_map)
    payload["account_id"] = settings.ynab_account_id
    payload["date"] = receipt.date.isoformat()
    payload["amount"] = -abs(_milliunits(receipt.grand_total))
    payload["payee_name"] = "Amazon"
    return payload


def match_order(order_id: str) -> None:
    row = db.get_order(order_id)
    if row is None or row["parse_status"] != "parsed":
        return

    receipt = Receipt.model_validate_json(row["parsed_json"])
    try:
        candidates = find_candidates(receipt.date, _milliunits(receipt.grand_total))
    except Exception as exc:
        # A YNAB API failure (bad token, network blip, etc.) must not crash the
        # caller — whether that's the pipeline loop, a dashboard request, or a
        # dev reset action. Fail the order into 'error', not the whole request.
        logger.exception("Order %s: failed to fetch candidate transactions", order_id)
        db.mark_error(order_id, f"matching failed: {exc}")
        db.increment_retry_count(order_id)
        return

    if not candidates:
        logger.info("Order %s: no matching YNAB transaction found", order_id)
        db.set_match_result(order_id, "no_candidate")
        return

    # Stored for both branches: the Review page shows this alongside the parsed
    # receipt even for the single-candidate case (docs/DESIGN.md §6) — approval is
    # checked against the actual transaction, not the parse in isolation.
    candidate_summaries = [
        {"id": t["id"], "date": t["date"], "amount": t["amount"], "payee": t.get("payee_name")}
        for t in candidates
    ]

    if len(candidates) == 1:
        txn = candidates[0]
        payload = build_patch_payload(receipt, order_id, load_categories_map())
        db.set_match_result(
            order_id,
            "pending_review",
            selected_txn_id=txn["id"],
            patch_payload_json=json.dumps(payload),
            candidate_ids_json=json.dumps(candidate_summaries),
        )
        return

    logger.info("Order %s: %d ambiguous candidates", order_id, len(candidates))
    db.set_match_result(order_id, "ambiguous", candidate_ids_json=json.dumps(candidate_summaries))


def pick_candidate(order_id: str, txn_id: str) -> None:
    """Manual disambiguation: user picked one of the ambiguous candidates. Demotes
    the order to pending_review — it still requires an explicit Approve."""
    row = db.get_order(order_id)
    if row is None:
        return
    receipt = Receipt.model_validate_json(row["parsed_json"])
    payload = build_patch_payload(receipt, order_id, load_categories_map())
    db.set_match_result(
        order_id,
        "pending_review",
        selected_txn_id=txn_id,
        patch_payload_json=json.dumps(payload),
        candidate_ids_json=row["candidate_ynab_txn_ids"],  # preserve the candidate list shown
    )
