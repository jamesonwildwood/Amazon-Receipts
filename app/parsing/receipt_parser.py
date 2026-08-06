import re

from bs4 import BeautifulSoup

from app.models import Receipt
from app.parsing.llm_client import get_provider


def receipt_html_to_text(html: str) -> str:
    """Ported from vendor/ynab_amazon/main.py's _receipt_html_to_txt."""
    soup = BeautifulSoup(html, features="html.parser")
    return re.sub(r"\n\s+", "\n", soup.text)


def parse_receipt_html(html: str, category_names: list[str]) -> Receipt:
    receipt_text = receipt_html_to_text(html)
    provider = get_provider()
    raw = provider.extract_receipt(receipt_text, category_names + ["other"])
    return Receipt.model_validate(raw)
