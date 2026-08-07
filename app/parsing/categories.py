import requests

from app.config import settings
from app.models import Category

# Groups that exist in every YNAB budget but aren't real spending categories, so
# they should never be offered to the LLM as something a purchase could belong to:
# - "Internal Master Category": YNAB's internal bookkeeping (e.g. "Uncategorized").
# - "Credit Card Payments": auto-generated debt-paydown tracking, one per card —
#   assigning a purchase here wouldn't make sense in YNAB's model.
_EXCLUDED_GROUP_NAMES = {"internal master category", "credit card payments"}


def get_ynab_categories() -> list[Category]:
    """Reused from vendor/ynab_amazon/ynab.py's get_categories, but offering every
    real category in the budget instead of gating behind an opt-in "[Auto]" group
    prefix — per Jameson's request to just use the categories he already has."""
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {settings.ynab_personal_access_token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(
        f"https://api.ynab.com/v1/budgets/{settings.ynab_budget_id}/categories",
        headers=headers,
        timeout=(5, 30),
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        Category(group=cg["name"], name=c["name"], category_id=c["id"])
        for cg in data["data"]["category_groups"]
        for c in cg["categories"]
        if cg["name"].lower() not in _EXCLUDED_GROUP_NAMES
        and not c.get("hidden")
        and not c.get("deleted")
    ]


def categories_by_name(categories: list[Category]) -> dict[str, Category]:
    return {c.get_name(): c for c in categories}
