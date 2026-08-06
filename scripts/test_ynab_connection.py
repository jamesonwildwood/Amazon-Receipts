#!/usr/bin/env python
"""Quick YNAB connectivity check: verifies YNAB_PERSONAL_ACCESS_TOKEN and
YNAB_BUDGET_ID resolve, lists accounts (to help you find YNAB_ACCOUNT_ID if
not set yet), and lists the real budget categories that will be offered to
the LLM for categorization. Never prints the token itself."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.parsing.categories import get_ynab_categories
from app.ynab.client import get_accounts


def main() -> None:
    if not settings.ynab_personal_access_token:
        print("YNAB_PERSONAL_ACCESS_TOKEN is empty in .env — nothing to test.")
        sys.exit(1)

    print(f"Budget ID: {settings.ynab_budget_id!r}")

    try:
        accounts = get_accounts()
    except Exception as exc:
        print(f"FAILED to reach YNAB: {exc}")
        sys.exit(1)

    print(f"Connected OK. {len(accounts)} account(s) in this budget:")
    for a in accounts:
        marker = " <- YNAB_ACCOUNT_ID currently set to this" if a["id"] == settings.ynab_account_id else ""
        closed = " (closed)" if a.get("closed") else ""
        print(f"  {a['id']}  {a['name']!r}{closed}{marker}")

    if not settings.ynab_account_id:
        print("\nYNAB_ACCOUNT_ID is not set in .env yet — copy the id of your Amazon card above.")
    elif settings.ynab_account_id not in {a["id"] for a in accounts}:
        print(f"\nWARNING: YNAB_ACCOUNT_ID={settings.ynab_account_id!r} does not match any account above.")

    try:
        categories = get_ynab_categories()
        print(f"\n{len(categories)} categor{'y' if len(categories) == 1 else 'ies'} available for auto-categorization:")
        for c in categories:
            print(f"  {c.group} / {c.name}  ({c.category_id})")
        if not categories:
            print("  None found — check that this budget has at least one non-hidden category.")
    except Exception as exc:
        print(f"\nCategory lookup failed: {exc}")


if __name__ == "__main__":
    main()
