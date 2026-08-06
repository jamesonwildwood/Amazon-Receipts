#!/usr/bin/env python
"""Dev/test CLI for one order — calls the exact same app/ynab/apply.py functions
the dashboard uses, so there is one code path whether triggered from the GUI or
here. Requires ALLOW_RESET=true / ALLOW_REAPPLY=true in .env for the respective
action, same as the dashboard controls."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db
from app.logging_setup import configure_logging
from app.ynab.apply import apply_patch, reset_order


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--action", required=True, choices=["reset-review", "reset-reparse", "reapply"])
    args = parser.parse_args()

    configure_logging()
    init_db()

    if args.action == "reset-review":
        ok = reset_order(args.order_id, "pending_review")
        print("Reset to pending_review (re-matched):" if ok else "Reset refused (check ALLOW_RESET and order status):", ok)
    elif args.action == "reset-reparse":
        ok = reset_order(args.order_id, "pending_parse")
        print("Reset to pending_parse (re-parsed + re-matched):" if ok else "Reset refused (check ALLOW_RESET and order status):", ok)
    elif args.action == "reapply":
        result = apply_patch(args.order_id, allow_reapply=True)
        print(f"apply_patch(allow_reapply=True) -> ok={result.ok} reason={result.reason} txn={result.ynab_transaction_id}")


if __name__ == "__main__":
    main()
