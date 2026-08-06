import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

# Ported from vendor/ynab_amazon/models.py
# https://stackoverflow.com/questions/33404752/removing-emojis-from-a-string-in-python
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002500-\U00002BEF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001f926-\U0001f937"
    "\U00010000-\U0010ffff"
    "♀-♂"
    "☀-⭕"
    "‍"
    "⏏"
    "⏩"
    "⌚"
    "️"
    "〰"
    "]+",
    re.UNICODE,
)


@dataclass
class Category:
    group: str
    name: str
    category_id: str

    def get_name(self) -> str:
        return EMOJI_PATTERN.sub("", self.name).strip().lower()


def resolve_item_category(
    item_category_name: str, categories_by_name: dict[str, Category]
) -> Optional[Category]:
    """Resolve an LLM-guessed category name against the user's real YNAB categories.

    This replaces vendor/ynab_amazon's Item.set_ynab_category, which had a bug: it set
    self._ynab_category to the match and then unconditionally overwrote it back to None
    on the very next line, so a match never actually stuck. Being a plain function that
    returns its result (instead of mutating hidden state) makes that class of bug
    structurally harder to reintroduce.
    """
    normalized = EMOJI_PATTERN.sub("", item_category_name).strip().lower()
    return categories_by_name.get(normalized)


class Item(BaseModel):
    price: Decimal
    title: str
    short_name: str
    category: str

    def adjusted_cost(self, receipt: "Receipt") -> Decimal:
        """The "adjusted cost" accounts for tax/discounts applied evenly across items."""
        item_subtotal = receipt.item_subtotal()
        if item_subtotal == 0:
            return Decimal("0")
        return round(self.price / item_subtotal * receipt.grand_total, 2)


class Receipt(BaseModel):
    items: list[Item]
    total_before_tax: Decimal
    subtotal: Decimal
    grand_total: Decimal
    date: dt.date

    def item_subtotal(self) -> Decimal:
        return sum((i.price for i in self.items), Decimal("0"))
