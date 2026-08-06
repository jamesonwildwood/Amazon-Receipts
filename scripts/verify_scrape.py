#!/usr/bin/env python
"""Phase 1 manual check: run the scraper only (no parsing/matching) and report
what was found. See docs/DESIGN.md Phase 1 for what "success" looks like:
login incl. OTP succeeds, N receipt files saved under data/receipts_html/,
N rows inserted into amazon_orders."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db
from app.logging_setup import configure_logging
from app.scraper.wrapper import scrape_new_orders

configure_logging()
logger = logging.getLogger(__name__)


def main() -> None:
    init_db()
    new_order_ids = scrape_new_orders()
    if not new_order_ids:
        logger.info("No new orders found (either none exist, or all were already scraped).")
    else:
        logger.info("Scraped %d new order(s): %s", len(new_order_ids), ", ".join(new_order_ids))


if __name__ == "__main__":
    main()
