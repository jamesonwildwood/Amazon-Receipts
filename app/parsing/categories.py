import requests

from app.config import settings
from app.models import Category


def get_auto_categories() -> list[Category]:
    """Reused from vendor/ynab_amazon/ynab.py's get_categories, generalized to a
    configurable group prefix (YNAB_AUTO_CATEGORY_GROUP_PREFIX) instead of the
    hardcoded "[Auto]"."""
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {settings.ynab_personal_access_token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(
        f"https://api.ynab.com/v1/budgets/{settings.ynab_budget_id}/categories",
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    prefix = settings.ynab_auto_category_group_prefix.lower()
    return [
        Category(group=cg["name"], name=c["name"], category_id=c["id"])
        for cg in data["data"]["category_groups"]
        for c in cg["categories"]
        if cg["name"].lower().startswith(prefix)
    ]


def categories_by_name(categories: list[Category]) -> dict[str, Category]:
    return {c.get_name(): c for c in categories}
