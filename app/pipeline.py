import datetime as dt
import fcntl
import json
import logging
from pathlib import Path
from typing import IO, Optional

from app import db, health, notify
from app.accounts import load_accounts, ynab_account_id_for_label
from app.config import settings
from app.dashboard.formatting import format_cents
from app.parsing.categories import categories_by_name, get_ynab_categories
from app.parsing.receipt_parser import parse_receipt_html
from app.scraper.wrapper import scrape_new_orders
from app.ynab.apply import apply_patch
from app.ynab.matcher import fetch_transactions_for_window, match_order

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


def _lock_path() -> Path:
    return Path(settings.database_path).resolve().parent / "pipeline.lock"


def _acquire_lock() -> Optional[IO]:
    """Non-blocking OS-level lock so a CLI run (app/__main__.py) and the
    server's scheduled/Run-Now run -- separate processes, not just separate
    threads -- can never overlap into two simultaneous Selenium logins
    (docs/IMPROVEMENTS.md 3.7). Replaces the old in-process threading.Lock,
    which only guarded threads within one process. flock() is scoped per
    *open file description*, not per process, so this correctly also blocks
    a second caller within the same process (e.g. Run Now racing a cron
    fire) as long as each caller does its own open() -- never share one
    module-level file handle across callers.

    Returns the open file handle (caller must keep it open and pass it to
    _release_lock later) or None if another run already holds it."""
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def _release_lock(lock_file: IO) -> None:
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()


def run_pipeline(account_label: Optional[str] = None, headless: Optional[bool] = None) -> Optional[int]:
    """Runs scrape -> parse -> match end to end, recording a pipeline_runs row.
    Returns that row's id, or None if a run was already in progress (in which
    case this call did nothing).

    account_label restricts scraping to one configured account (the CLI's
    --account, docs/IMPROVEMENTS.md 3.6); None scrapes every configured
    account. headless overrides SCRAPE_HEADLESS for this run only (the CLI's
    --headful); None uses the configured setting."""
    lock_file = _acquire_lock()
    if lock_file is None:
        logger.warning("run_pipeline() called while a run is already in progress — skipping")
        return None

    try:
        return _run_pipeline_locked(account_label=account_label, headless=headless)
    finally:
        _release_lock(lock_file)


def _run_pipeline_locked(account_label: Optional[str] = None, headless: Optional[bool] = None) -> int:
    run_id = db.start_run()
    orders_found = orders_parsed = orders_matched = 0
    status = "success"
    error_message = None
    failed_accounts: list[str] = []
    new_pending_order_ids: list[str] = []
    auto_applied_orders: list[dict] = []

    try:
        # Cheap authenticated checks that catch a revoked YNAB token or a
        # broken amazon_accounts.toml before every order in this run fails
        # against it individually (docs/IMPROVEMENTS.md 5.5). Each check
        # already catches its own exceptions and never raises; this extra
        # try/except only guards against a bug in the health module itself.
        try:
            health.run_startup_checks()
        except Exception:
            logger.exception("Config sanity checks failed unexpectedly at pipeline-run start")

        accounts = load_accounts()
        if account_label is not None:
            accounts = [a for a in accounts if a.label == account_label]
            if not accounts:
                raise ValueError(f"no configured Amazon account with label {account_label!r}")

        if not accounts:
            logger.warning(
                "No Amazon accounts configured (amazon_accounts.toml absent and "
                "AMAZON_EMAIL/AMAZON_PASSWORD unset) — skipping scrape"
            )

        # Accounts scraped sequentially, inside this same lock -- never two
        # Selenium logins at once (docs/IMPROVEMENTS.md 3.3). A failure on one
        # account is caught here and marks the run 'partial' rather than
        # aborting the others; per-account detail goes to the log (this
        # exception) and the dashboard's integration-health card (3.5), not
        # into orders_found/orders_parsed/orders_matched, which stay aggregate.
        for account in accounts:
            try:
                new_ids = scrape_new_orders(account, headless=headless)
                orders_found += len(new_ids)
            except Exception:
                logger.exception("Scrape failed for Amazon account %r", account.label)
                failed_accounts.append(account.label)
                status = "partial"

        # Fetched once for the whole run, not once per order (docs/IMPROVEMENTS.md
        # item 5) -- YNAB's rate limit is 200 requests/hour per token, and a
        # per-order categories call alone was enough to burn through it during a
        # live batch run tonight.
        categories = get_ynab_categories() if settings.ynab_personal_access_token else []
        category_names = [c.get_name() for c in categories]
        categories_map = categories_by_name(categories)

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

        # One transaction fetch per *distinct resolved YNAB account id*, not
        # per order and not per Amazon account (docs/IMPROVEMENTS.md 3.4) --
        # two Amazon accounts commonly share one YNAB account, so fetching
        # per Amazon account would just repeat the same YNAB call. Falls back
        # to match_order's own per-order fetch (transactions=None) for any
        # group whose batched call fails, so one network hiccup doesn't abort
        # matching for the whole run.
        accounts_config = load_accounts()
        order_rows = {oid: db.get_order(oid) for oid in to_match}
        ynab_account_by_order = {
            oid: ynab_account_id_for_label(row["amazon_account"], accounts_config)
            for oid, row in order_rows.items()
            if row is not None
        }

        orders_by_ynab_account: dict[str, list[str]] = {}
        for oid, ynab_account_id in ynab_account_by_order.items():
            orders_by_ynab_account.setdefault(ynab_account_id, []).append(oid)

        transactions_by_ynab_account: dict[str, list[dict]] = {}
        for ynab_account_id, order_ids_for_account in orders_by_ynab_account.items():
            order_dates = [
                dt.date.fromisoformat(order_rows[oid]["order_date"])
                for oid in order_ids_for_account
                if order_rows[oid]["order_date"]
            ]
            if not order_dates:
                continue
            since_date = min(order_dates) - dt.timedelta(days=settings.ynab_match_window_days)
            try:
                transactions_by_ynab_account[ynab_account_id] = fetch_transactions_for_window(
                    since_date, ynab_account_id=ynab_account_id
                )
            except Exception:
                logger.exception(
                    "Batch transaction fetch failed for YNAB account %s; falling back to per-order fetch this run",
                    ynab_account_id,
                )

        for order_id in to_match:
            try:
                ynab_account_id = ynab_account_by_order.get(order_id)
                match_order(
                    order_id,
                    transactions=transactions_by_ynab_account.get(ynab_account_id),
                    categories_map=categories_map,
                )
            except Exception:
                logger.exception("Failed to match order %s", order_id)
                status = "partial"
                continue

            row = db.get_order(order_id)
            if row is None or row["match_status"] not in ("pending_review", "ambiguous"):
                # no_candidate / error -- not a real match. orders_matched
                # counts real matches, not attempts (docs/IMPROVEMENTS.md Extra 2).
                continue
            orders_matched += 1

            if row["match_status"] == "pending_review" and settings.ynab_auto_apply:
                # Opt-in (docs/IMPROVEMENTS.md 5.2): a single-candidate match
                # is applied immediately through the exact same guarded
                # apply_patch() the dashboard's Approve button uses -- no new
                # write path, every existing guard (atomic claim, re-fetch,
                # amount verification, claim ledger) still applies unchanged.
                # Ambiguous matches never reach this branch; a guard refusal
                # (amount changed, already claimed, transaction reconciled,
                # etc.) leaves the order at 'error' for a human, same as a
                # manual Approve failing would -- it still shows up in the
                # digest below via new_pending_order_ids.
                apply_result = apply_patch(order_id)
                if apply_result.ok and apply_result.reason == "applied":
                    candidates = json.loads(row["candidate_ynab_txn_ids"]) if row["candidate_ynab_txn_ids"] else []
                    auto_applied_orders.append(
                        {
                            "order_id": order_id,
                            "amazon_account": row["amazon_account"],
                            "grand_total_cents": row["grand_total_cents"],
                            "matched_txn_date": candidates[0]["date"] if candidates else None,
                        }
                    )
                    continue

            new_pending_order_ids.append(order_id)

    except Exception as exc:
        logger.exception("Pipeline run failed")
        status = "error"
        error_message = str(exc)
    finally:
        if failed_accounts and not error_message:
            error_message = f"scrape failed for account(s): {', '.join(failed_accounts)} — see logs"
        db.finish_run(run_id, status, orders_found, orders_parsed, orders_matched, error_message)

        # A notifier failure must never fail or delay the pipeline
        # (docs/IMPROVEMENTS.md 5.1) -- this run's own row is already
        # recorded above regardless of what happens next.
        try:
            _send_run_notifications(status, error_message, new_pending_order_ids, auto_applied_orders)
        except Exception:
            logger.exception("Pipeline notification failed (never affects the run itself)")

        if status == "success":
            # Backups ride along with a healthy run only -- a partial/error
            # run's DB state isn't worth enshrining as "the" backup for that
            # day (docs/IMPROVEMENTS.md Extra 1). Never allowed to fail the run.
            try:
                db.backup_database()
            except Exception:
                logger.exception("Database backup failed after pipeline run")

    return run_id


def _send_run_notifications(
    status: str,
    error_message: Optional[str],
    new_pending_order_ids: list[str],
    auto_applied_orders: list[dict],
) -> None:
    """Composes and fires the docs/IMPROVEMENTS.md 5.1 emails for one
    pipeline run: an immediate one on error/partial, and a separate digest
    when the run left new items in pending_review/ambiguous and/or
    auto-applied anything. Silent on a healthy run with neither."""
    dashboard_url = settings.notify_dashboard_url

    if status in ("error", "partial"):
        notify.send_email(
            f"Amazon Receipts: pipeline run {status}",
            f"Status: {status}\n\n{error_message or '(no error message recorded)'}\n\nDashboard: {dashboard_url}",
        )

    if not new_pending_order_ids and not auto_applied_orders:
        return

    lines = []
    if new_pending_order_ids:
        lines.append(f"{len(new_pending_order_ids)} order(s) waiting for review — {dashboard_url}/review")
    if auto_applied_orders:
        lines.append(f"\nAuto-applied {len(auto_applied_orders)} order(s):")
        for order in auto_applied_orders:
            amount = format_cents(order["grand_total_cents"])
            lines.append(
                f"  - {order['order_id']} ({order['amazon_account']}): {amount}, "
                f"matched transaction dated {order['matched_txn_date'] or 'unknown'}"
            )
    notify.send_email("Amazon Receipts: pending work / auto-applied", "\n".join(lines))
