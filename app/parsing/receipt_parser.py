import re

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


def parse_receipt_html(html: str, category_names: list[str]) -> Receipt:
    receipt_text = _strip_shipping_address(receipt_html_to_text(html))
    provider = get_provider()
    raw = provider.extract_receipt(receipt_text, category_names + ["other"])
    return Receipt.model_validate(raw)
