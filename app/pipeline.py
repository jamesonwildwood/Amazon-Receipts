import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Optional

from app import db
from app.config import settings
from app.parsing.categories import get_ynab_categories
from app.parsing.receipt_parser import parse_receipt_html
from app.scraper.wrapper import scrape_new_orders
from app.ynab.matcher import match_order

# How far back to keep retrying a no_candidate order on each run. Amazon
# typically charges at shipment (1-15+ days after ordering, occasionally more
# — see docs/IMPROVEMENTS.md item 1), so a single match attempt right after
# scraping often finds nothing yet. Padded past the match window itself so a
# late-settling charge still gets picked up on a later run.
_NO_CANDIDATE_RETRY_PAD_DAYS = 10

# Bounds automatic retry of match-time errors (a network blip fetching
# candidates, etc.) — apply-time errors are never included here regardless of
# this bound, since they need human eyes (see db.list_retryable_match_error_order_ids).
_MAX_MATCH_RETRIES = 5

# A run older than this is treated as stale/crashed rather than genuinely
# in-progress — see docs/IMPROVEMENTS.md item 3 and db.mark_stale_runs_as_error().
STALE_RUN_AFTER_HOURS = 2

logger = logging.getLogger(__name__)

# Guards against two concurrent run_pipeline() calls (Run Now racing the
# scheduler's cron fire, or a double-click) driving two Selenium logins into
# the same Amazon account at once and racing on insert_scraped_order. The
# scheduler's own max_instances=1 only guards cron-vs-cron, not Run Now.
_run_lock = threading.Lock()


def run_pipeline() -> Optional[int]:
    """Runs scrape -> parse -> match end to end, recording a pipeline_runs row.
    Returns that row's id, or None if a run was already in progress (in which
    case this call did nothing)."""
    if not _run_lock.acquire(blocking=False):
        logger.warning("run_pipeline() called while a run is already in progress — skipping")
        return None

    try:
        return _run_pipeline_locked()
    finally:
        _run_lock.release()


def _run_pipeline_locked() -> int:
    run_id = db.start_run()
    orders_found = orders_parsed = orders_matched = 0
    status = "success"
    error_message = None

    try:
        new_order_ids = scrape_new_orders()
        orders_found = len(new_order_ids)

        category_names = (
            [c.get_name() for c in get_ynab_categories()] if settings.ynab_personal_access_token else []
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

        retry_cutoff = (
            dt.date.today()
            - dt.timedelta(days=settings.ynab_match_window_days + _NO_CANDIDATE_RETRY_PAD_DAYS)
        ).isoformat()
        to_match = (
            db.list_pending_match_order_ids()
            + db.list_no_candidate_order_ids_since(retry_cutoff)
            + db.list_retryable_match_error_order_ids(_MAX_MATCH_RETRIES)
        )

        for order_id in to_match:
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
