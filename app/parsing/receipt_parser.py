import re
from decimal import Decimal
from typing import Optional

from bs4 import BeautifulSoup

from app.models import Receipt
from app.parsing.llm_client import get_provider

# Amazon's order-detail page always renders a "Ship to" block (name + street
# address + country) immediately before "Payment method". Stripped only from
# the text sent to the LLM — the saved raw HTML file is left untouched as the
# audit trail. Non-greedy so a multi-shipment order's repeated blocks each get
# stripped independently rather than one match swallowing everything between
# the first "Ship to" and the last "Payment method".
_SHIP_TO_BLOCK = re.compile(r"Ship to\n.*?\n(?=Payment method)", re.DOTALL)


def receipt_html_to_text(html: str) -> str:
    """Ported from vendor/ynab_amazon/main.py's _receipt_html_to_txt."""
    soup = BeautifulSoup(html, features="html.parser")
    return re.sub(r"\n\s+", "\n", soup.text)


def _strip_shipping_address(text: str) -> str:
    return _SHIP_TO_BLOCK.sub("", text)


# Amazon's print-view invoice renders the charged total as a "Grand Total:"
# label with the amount on the following text line. Cheap and deterministic,
# so the refresh pass (app/scraper/wrapper.py) can tell whether a receipt's
# total changed since it was scraped *without* spending an LLM call on it.
_GRAND_TOTAL = re.compile(r"Grand Total:\s*\$?([\d,]+\.\d{2})")
_CANCELLED = "Your order was cancelled"


def extract_grand_total(html: str) -> Optional[Decimal]:
    """The charged total as printed on the invoice, or None when the page has
    no "Grand Total" line -- which is what a cancelled or still-unshipped
    Subscribe & Save order renders."""
    match = _GRAND_TOTAL.search(receipt_html_to_text(html))
    return Decimal(match.group(1).replace(",", "")) if match else None


def is_cancelled(html: str) -> bool:
    return _CANCELLED in receipt_html_to_text(html)


def parse_receipt_html(html: str, category_names: list[str]) -> Receipt:
    receipt_text = _strip_shipping_address(receipt_html_to_text(html))
    provider = get_provider()
    raw = provider.extract_receipt(receipt_text, category_names + ["other"])
    return Receipt.model_validate(raw)
