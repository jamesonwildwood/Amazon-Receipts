import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "vendor" / "amazon_orders_webscraper"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import pages as amazon_pages  # noqa: E402  (vendored page objects, see vendor/amazon_orders_webscraper)

from app import db
from app.accounts import AmazonAccount
from app.config import settings

logger = logging.getLogger(__name__)


def _signin(driver, account: AmazonAccount) -> None:
    logger.info("Loading Amazon sign-in page for account %r", account.label)
    email_page = amazon_pages.PrimeLoginEmailPage(driver)
    email_page.load()
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


def scrape_new_orders(account: AmazonAccount, headless: bool | None = None) -> list[str]:
    """Logs into one Amazon account, discovers order ids across order-history
    pages, and downloads the receipt for any order not already recorded in
    the DB. Returns the list of newly-scraped order ids.

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
    finally:
        driver.quit()

    return new_order_ids
