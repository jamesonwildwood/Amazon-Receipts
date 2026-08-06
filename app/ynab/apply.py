import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import db
from app.config import settings
from app.ynab import client as ynab_client

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    ok: bool
    reason: str
    ynab_transaction_id: Optional[str] = None


def apply_patch(order_id: str, allow_reapply: bool = False) -> ApplyResult:
    """The one guarded routine that writes to YNAB — used by the dashboard's
    Approve/Force-Re-Apply actions and by scripts/reprocess_order.py, so there is
    exactly one code path. See docs/DESIGN.md §5 for the numbered steps this follows."""
    if allow_reapply and not settings.allow_reapply:
        return ApplyResult(False, "reapply_disabled")

    row = db.get_order(order_id)
    if row is None:
        return ApplyResult(False, "order_not_found")

    # 1. Duplicate guard — refuse before touching YNAB at all.
    if row["match_status"] == "approved" and row["ynab_transaction_id_patched"] and not allow_reapply:
        return ApplyResult(True, "already_applied", row["ynab_transaction_id_patched"])

    # 2. Atomic claim — makes Approve idempotent against a double-click or an
    #    overlapping scheduler run.
    allowed_from = ("pending_review",) if not allow_reapply else ("pending_review", "approved")
    if not db.claim_for_apply(order_id, allowed_from):
        return ApplyResult(False, "already_processing")

    txn_id = row["selected_ynab_txn_id"]
    payload_json = row["ynab_patch_payload"]
    if not txn_id or not payload_json:
        db.mark_error(order_id, "no staged transaction/payload to apply")
        return ApplyResult(False, "nothing_staged")

    # 3. Re-fetch the transaction fresh — guards against it being edited/
    #    reconciled/deleted since the matcher last saw it.
    try:
        fresh_txn = ynab_client.get_transaction(txn_id)
    except Exception as exc:
        db.mark_error(order_id, f"failed to re-fetch transaction: {exc}")
        return ApplyResult(False, "refetch_failed")

    if fresh_txn.get("deleted"):
        db.mark_error(order_id, "transaction was deleted since match")
        return ApplyResult(False, "transaction_deleted")

    # 4. Final double-claim check at write time, not just at match time.
    other = db.find_order_bound_to(txn_id, exclude_order_id=order_id)
    if other is not None:
        db.mark_error(order_id, f"transaction already claimed by order {other['order_id']}")
        return ApplyResult(False, "transaction_already_claimed")

    # 5. PATCH the full desired state (never a partial/append).
    payload = json.loads(payload_json)
    try:
        ynab_client.patch_transaction(txn_id, payload)
        success, error_message = True, None
    except Exception as exc:
        success, error_message = False, str(exc)

    # 6. Log the attempt unconditionally.
    db.log_apply_attempt(
        order_id, txn_id, payload_json, is_reapply=allow_reapply, success=success, error_message=error_message
    )

    if success:
        db.mark_approved(order_id, txn_id, payload_json)
        return ApplyResult(True, "applied", txn_id)

    db.mark_error(order_id, error_message)
    return ApplyResult(False, "patch_failed")


def reset_order(order_id: str, target: str) -> bool:
    """Dev-only: resets local state and re-runs matching (and optionally re-parsing)
    for one order, without ever touching YNAB and without clearing apply history
    (ynab_transaction_id_patched/ynab_patched_at/apply_count/ynab_apply_log stay
    visible so you can see what was applied while you re-test)."""
    if not settings.allow_reset:
        return False
    if target not in ("pending_review", "pending_parse"):
        raise ValueError(f"invalid reset target: {target}")

    row = db.get_order(order_id)
    if row is None or row["match_status"] not in ("approved", "rejected", "error", "no_candidate", "ambiguous"):
        return False

    if target == "pending_parse":
        from app.parsing.categories import get_auto_categories
        from app.parsing.receipt_parser import parse_receipt_html

        try:
            category_names = (
                [c.get_name() for c in get_auto_categories()] if settings.ynab_personal_access_token else []
            )
            html = Path(row["html_path"]).read_text()
            receipt = parse_receipt_html(html, category_names)
            db.update_parsed(order_id, receipt)
        except Exception as exc:
            # A re-parse failure (bad LLM key, malformed response, etc.) must not
            # crash the caller. Record it and stop — there's no fresh parsed_json
            # to match against.
            logger.exception("Reset: re-parse failed for order %s", order_id)
            db.mark_parse_error(order_id, f"re-parse failed: {exc}")
            return True  # the reset action itself ran; the reparse attempt failed and is recorded

    from app.ynab.matcher import match_order

    match_order(order_id)
    return True
