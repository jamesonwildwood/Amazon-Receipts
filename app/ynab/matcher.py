import datetime as dt
import json
import logging

from app import db
from app.accounts import ynab_account_id_for_label
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


def _filter_candidates(transactions: list[dict], order_date: dt.date, grand_total_milliunits: int) -> list[dict]:
    """Pure filtering over an already-fetched transaction list: a window around
    the order date, exact amount match, uncategorized-only and payee filters
    (both configurable), excluding any transaction already bound to a different
    approved order. Split out from find_candidates() so a pipeline run can fetch
    transactions once and filter per-order locally, instead of one YNAB API call
    per order (docs/IMPROVEMENTS.md item 5 -- this is the exact rate-limit issue
    a live batch run hit).

    Must enforce BOTH ends of the window itself: the batch path
    (app/pipeline.py) fetches one shared transaction list bounded by the
    *earliest* order in the batch, which is wider than any single order's own
    window -- a transaction from months before this specific order's window
    can legitimately be sitting in that shared list. find_candidates() (the
    single-order path) happens to pre-scope its fetch to since_date=window_start,
    but that's an artifact of how it fetches, not a substitute for this
    function checking its own bound on whatever list it's handed."""
    window_start = order_date - dt.timedelta(days=settings.ynab_match_window_days)
    window_end = order_date + dt.timedelta(days=settings.ynab_match_window_days)
    already_bound = db.bound_transaction_ids()
    payee_filters = settings.ynab_amazon_payee_filter_list

    candidates = []
    for txn in transactions:
        if txn.get("deleted"):
            continue
        txn_date = dt.date.fromisoformat(txn["date"])
        if txn_date < window_start or txn_date > window_end:
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


def fetch_transactions_for_window(since_date: dt.date, ynab_account_id: str | None = None) -> list[dict]:
    """Fetches every transaction from since_date to now, once, for one YNAB
    account. Callers filter the shared result per-order with
    _filter_candidates() rather than each calling get_transactions_since()
    themselves. ynab_account_id defaults to the global YNAB_ACCOUNT_ID; the
    pipeline's batch path (docs/IMPROVEMENTS.md 3.4) fetches per *distinct*
    resolved YNAB account id, not per Amazon account, since two Amazon
    accounts usually share one YNAB account."""
    ynab_account_id = ynab_account_id or settings.ynab_account_id
    return ynab_client.get_transactions_since(ynab_account_id, since_date.isoformat())


def find_candidates(
    order_date: dt.date, grand_total_milliunits: int, ynab_account_id: str | None = None
) -> list[dict]:
    """Single-order fetch + filter — used by the dashboard's pick_candidate/
    reset_order paths, where there's only ever one order to match and batching
    wouldn't help. The pipeline's batch path (match_order with a pre-fetched
    transactions list) skips the fetch here entirely; see docs/IMPROVEMENTS.md
    item 5."""
    window_start = order_date - dt.timedelta(days=settings.ynab_match_window_days)
    transactions = fetch_transactions_for_window(window_start, ynab_account_id=ynab_account_id)
    return _filter_candidates(transactions, order_date, grand_total_milliunits)


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


def build_create_payload(
    receipt: Receipt, order_id: str, categories_map: dict, ynab_account_id: str | None = None
) -> dict:
    """Full payload for creating a brand-new transaction — used only for the
    no-bank-match backfill path (app/ynab/apply.py:create_transaction), when
    match_status == 'no_candidate'. Unlike build_patch_payload, this must
    include account_id/date/amount/payee_name since there's no existing
    transaction to inherit them from. ynab_account_id defaults to the global
    YNAB_ACCOUNT_ID; callers resolving a specific order pass its account's
    override (docs/IMPROVEMENTS.md 3.4)."""
    payload = build_patch_payload(receipt, order_id, categories_map)
    payload["account_id"] = ynab_account_id or settings.ynab_account_id
    payload["date"] = receipt.date.isoformat()
    payload["amount"] = -abs(_milliunits(receipt.grand_total))
    payload["payee_name"] = "Amazon"
    return payload


def match_order(
    order_id: str,
    transactions: list[dict] | None = None,
    categories_map: dict | None = None,
) -> None:
    """transactions: when given (the pipeline's batch path), filters this
    already-fetched list instead of calling YNAB per order. When None (the
    dashboard's single-order paths), falls back to find_candidates()'s own
    fetch. Same for categories_map vs. load_categories_map()."""
    row = db.get_order(order_id)
    if row is None or row["parse_status"] != "parsed":
        return

    receipt = Receipt.model_validate_json(row["parsed_json"])
    try:
        if transactions is not None:
            candidates = _filter_candidates(transactions, receipt.date, _milliunits(receipt.grand_total))
        else:
            # transactions is None on the dashboard's single-order paths (the
            # pipeline's batch path always supplies it) -- resolve this
            # order's own YNAB account (its override, else the global
            # default) rather than assuming the global one always applies.
            ynab_account_id = ynab_account_id_for_label(row["amazon_account"])
            candidates = find_candidates(
                receipt.date, _milliunits(receipt.grand_total), ynab_account_id=ynab_account_id
            )
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
        payload = build_patch_payload(receipt, order_id, categories_map if categories_map is not None else load_categories_map())
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
