#!/usr/bin/env python
"""Phase 1 manual check: run the scraper only (no parsing/matching) and report
what was found. See docs/DESIGN.md Phase 1 for what "success" looks like:
login incl. OTP succeeds, N receipt files saved under data/receipts_html/,
N rows inserted into amazon_orders."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.accounts import load_accounts
from app.db import init_db
from app.logging_setup import configure_logging
from app.scraper.wrapper import scrape_new_orders

configure_logging()
logger = logging.getLogger(__name__)


def main() -> None:
    init_db()
    accounts = load_accounts()
    if not accounts:
        logger.error(
            "No Amazon accounts configured -- set up amazon_accounts.toml "
            "(see amazon_accounts.toml.example) or AMAZON_EMAIL/AMAZON_PASSWORD in .env."
        )
        return

    for account in accounts:
        new_order_ids = scrape_new_orders(account)
        if not new_order_ids:
            logger.info(
                "Account %r: no new orders found (either none exist, or all were already scraped).",
                account.label,
            )
        else:
            logger.info(
                "Account %r: scraped %d new order(s): %s",
                account.label, len(new_order_ids), ", ".join(new_order_ids),
            )


if __name__ == "__main__":
    main()
