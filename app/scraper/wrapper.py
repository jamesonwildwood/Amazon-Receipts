import datetime as dt
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "vendor" / "amazon_orders_webscraper"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import pages as amazon_pages  # noqa: E402  (vendored page objects, see vendor/amazon_orders_webscraper)
from selenium.common.exceptions import TimeoutException  # noqa: E402

from app import db
from app.accounts import AmazonAccount
from app.config import settings
from app.parsing.receipt_parser import extract_grand_total, is_cancelled

logger = logging.getLogger(__name__)


def _signin(driver, account: AmazonAccount) -> None:
    logger.info("Loading Amazon sign-in page for account %r", account.label)
    email_page = amazon_pages.PrimeLoginEmailPage(driver)
    try:
        email_page.load()
    except TimeoutException:
        # No email form appeared — almost always because the persisted Chrome
        # profile still holds a live session, so Amazon redirected away from
        # the sign-in form. Confirm by loading order history (signed out, it
        # redirects back to sign-in and this times out too, surfacing a real
        # failure — retry with `python -m app run --headful` to inspect).
        amazon_pages.OrdersSummaryPage(driver).load()
        logger.info("Account %r: existing session still valid, skipping sign-in", account.label)
        return
    password_page = email_page.username(account.email)
    password_page.load()
    otp_page = password_page.password(account.password)
    try:
        otp_page.load()
    except Exception:
        logger.info("No TOTP challenge presented")
        return
    logger.info("Submitting TOTP")
    # Services commonly display a manual-entry TOTP secret in space-separated
    # groups of 4 for readability; strip all whitespace before base32-decoding it.
    totp_secret = "".join(account.totp_secret.split())
    otp_page.otp(totp_secret)


def refresh_outcome(stored_total_cents: Optional[int], live_html: str) -> str | Decimal:
    """Decides what a re-fetched receipt means for a no_candidate order.
    Returns the new Decimal total when the invoice now prints a different
    Grand Total than the one parsed before (Subscribe & Save is repriced at
    shipment -- deeper discount tier, tax recalculated -- so the charge the
    bank feed sees is routinely a few cents to a few dollars off the
    order-time estimate; this is the only way the exact-amount matcher can
    ever find it). Otherwise one of 'cancelled' (Amazon says it never
    charged), 'no_total' (not shipped yet, nothing to compare), or
    'unchanged' -- all of which must NOT trigger a re-parse, so a refresh
    costs one page load and no LLM call unless the total really moved."""
    if is_cancelled(live_html):
        return "cancelled"
    live_total = extract_grand_total(live_html)
    if live_total is None:
        return "no_total"
    if stored_total_cents is not None and int(round(live_total * 100)) == stored_total_cents:
        return "unchanged"
    return live_total


def _refresh_receipts(driver, account: AmazonAccount, order_ids: Iterable[str], receipts_dir: Path) -> list[str]:
    """Re-fetches the invoice for each still-unmatched order inside the
    already-signed-in session and, only when its Grand Total changed, swaps
    in the new HTML and resets the order to parse -> match again this run.
    The superseded file is kept beside it (audit trail), never deleted."""
    refreshed: list[str] = []
    for order_id in order_ids:
        row = db.get_order(order_id)
        if row is None or row["match_status"] != "no_candidate":
            continue
        try:
            order_page = amazon_pages.OrderPage(driver, order_id)
            order_page.load()
            live_html = str(order_page)
        except Exception:
            logger.exception("Receipt refresh failed for order %s (account %r)", order_id, account.label)
            continue

        outcome = refresh_outcome(row["grand_total_cents"], live_html)
        if isinstance(outcome, str):
            logger.info("Receipt refresh for order %s (account %r): %s", order_id, account.label, outcome)
            continue

        html_path = receipts_dir / f"{order_id}.html"
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if html_path.exists():
            html_path.rename(receipts_dir / f"{order_id}.superseded-{stamp}.html")
        html_path.write_text(live_html)
        if db.reset_for_reparse(order_id, str(html_path)):
            logger.info(
                "Receipt refresh for order %s (account %r): total changed %s -> %s, re-parsing",
                order_id, account.label,
                f"{row['grand_total_cents'] / 100:.2f}" if row["grand_total_cents"] is not None else "?",
                outcome,
            )
            refreshed.append(order_id)
    return refreshed


def scrape_new_orders(
    account: AmazonAccount, headless: bool | None = None, refresh_order_ids: Iterable[str] = ()
) -> list[str]:
    """Logs into one Amazon account, discovers order ids across order-history
    pages, and downloads the receipt for any order not already recorded in
    the DB. Returns the list of newly-scraped order ids.

    refresh_order_ids: this account's still-unmatched (no_candidate) orders,
    whose invoices are re-fetched in the same session afterwards -- see
    _refresh_receipts. Not part of the return value; a refreshed order is
    re-queued in the DB and flows through the run's normal parse/match stages.

    Each account gets its own Chrome profile subdirectory (docs/IMPROVEMENTS.md
    3.3) -- reusing one profile across two different Amazon logins would trip
    Amazon's returning-device logic, the exact problem the persisted profile
    was added to solve in the first place. headless overrides SCRAPE_HEADLESS
    for this call (the CLI's --headful flag, docs/IMPROVEMENTS.md 3.6)."""
    from app.scraper.driver import build_driver

    receipts_dir = Path(settings.receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    chrome_profile_dir = str(Path(settings.chrome_profile_dir) / account.label)

    driver = build_driver(chrome_profile_dir=chrome_profile_dir, headless=headless)
    new_order_ids: list[str] = []
    try:
        _signin(driver, account)

        all_order_ids: list[str] = []
        page = amazon_pages.OrdersSummaryPage(driver)
        page.load()
        while page is not None:
            all_order_ids.extend(oid for oid in page.get_order_ids() if oid)
            page = page.maybe_next_page()

        unscraped = [oid for oid in dict.fromkeys(all_order_ids) if not db.has_order(oid)]
        logger.info(
            "Account %r: found %d order(s), %d not yet scraped", account.label, len(all_order_ids), len(unscraped)
        )

        for order_id in unscraped:
            logger.info("Fetching receipt for order %s (account %r)", order_id, account.label)
            order_page = amazon_pages.OrderPage(driver, order_id)
            order_page.load()
            html_path = receipts_dir / f"{order_id}.html"
            html_path.write_text(str(order_page))
            db.insert_scraped_order(order_id, html_path=str(html_path), amazon_account=account.label)
            new_order_ids.append(order_id)

        refresh_order_ids = list(refresh_order_ids)
        if refresh_order_ids:
            logger.info(
                "Account %r: re-fetching %d unmatched receipt(s) for repriced totals",
                account.label, len(refresh_order_ids),
            )
            _refresh_receipts(driver, account, refresh_order_ids, receipts_dir)
    finally:
        driver.quit()

    return new_order_ids
