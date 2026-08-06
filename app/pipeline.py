import logging
from pathlib import Path

from app import db
from app.config import settings
from app.parsing.categories import get_auto_categories
from app.parsing.receipt_parser import parse_receipt_html
from app.scraper.wrapper import scrape_new_orders
from app.ynab.matcher import match_order

logger = logging.getLogger(__name__)


def run_pipeline() -> int:
    """Runs scrape -> parse -> match end to end, recording a pipeline_runs row.
    Returns that row's id."""
    run_id = db.start_run()
    orders_found = orders_parsed = orders_matched = 0
    status = "success"
    error_message = None

    try:
        new_order_ids = scrape_new_orders()
        orders_found = len(new_order_ids)

        category_names = (
            [c.get_name() for c in get_auto_categories()] if settings.ynab_personal_access_token else []
        )

        for order_id in db.list_pending_parse_order_ids():
            row = db.get_order(order_id)
            try:
                html = Path(row["html_path"]).read_text()
                receipt = parse_receipt_html(html, category_names)
                db.update_parsed(order_id, receipt)
                orders_parsed += 1
            except Exception:
                logger.exception("Failed to parse order %s", order_id)
                db.mark_parse_error(order_id, "parse failed, see logs")
                status = "partial"

        for order_id in db.list_pending_match_order_ids():
            try:
                match_order(order_id)
                orders_matched += 1
            except Exception:
                logger.exception("Failed to match order %s", order_id)
                status = "partial"

    except Exception as exc:
        logger.exception("Pipeline run failed")
        status = "error"
        error_message = str(exc)
    finally:
        db.finish_run(run_id, status, orders_found, orders_parsed, orders_matched, error_message)

    return run_id
