#!/usr/bin/env python
"""Phase 2 manual check: parse already-saved receipt HTML files (from
scripts/verify_scrape.py) with the configured LLM provider and print the
result. Requires a real LLM_PROVIDER/API key in .env, and YNAB credentials
if you want your real YNAB category names offered to the model — otherwise it
falls back to a generic category list. Manually eyeball accuracy against
the actual receipts before trusting this in the pipeline."""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.logging_setup import configure_logging
from app.parsing.categories import get_ynab_categories
from app.parsing.receipt_parser import parse_receipt_html

configure_logging()
logger = logging.getLogger(__name__)

FALLBACK_CATEGORIES = ["groceries", "electronics", "pet supplies", "office supplies", "household", "other"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only parse the first N receipts (by filename) — useful for a quick accuracy "
             "check before spending API calls on the whole batch.",
    )
    args = parser.parse_args()

    receipts_dir = Path(settings.receipts_dir)
    html_files = sorted(receipts_dir.glob("*.html"))
    if not html_files:
        logger.warning("No saved receipts found in %s. Run scripts/verify_scrape.py first.", receipts_dir)
        return
    if args.limit:
        html_files = html_files[: args.limit]

    if settings.ynab_personal_access_token:
        category_names = [c.get_name() for c in get_ynab_categories()]
        logger.info("Using %d real YNAB category name(s)", len(category_names))
    else:
        category_names = FALLBACK_CATEGORIES
        logger.info("No YNAB_PERSONAL_ACCESS_TOKEN set; using fallback category list")

    for html_path in html_files:
        logger.info("Parsing %s", html_path.name)
        try:
            receipt = parse_receipt_html(html_path.read_text(), category_names)
        except Exception as exc:
            logger.error("Failed to parse %s: %s", html_path.name, exc)
            continue
        print(f"--- {html_path.name} ---")
        print(json.dumps(receipt.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
