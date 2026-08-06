import pytest

from app.config import settings
from app.parsing import categories


@pytest.fixture
def mock_categories_response(monkeypatch):
    payload = {
        "data": {
            "category_groups": [
                {
                    "name": "Internal Master Category",
                    "categories": [
                        {"id": "cat-uncategorized", "name": "Uncategorized", "hidden": False, "deleted": False},
                    ],
                },
                {
                    "name": "Household",
                    "categories": [
                        {"id": "cat-pet", "name": "Pet Supplies", "hidden": False, "deleted": False},
                        {"id": "cat-hidden", "name": "Old Category", "hidden": True, "deleted": False},
                        {"id": "cat-deleted", "name": "Removed Category", "hidden": False, "deleted": True},
                    ],
                },
                {
                    "name": "Everyday Expenses",
                    "categories": [
                        {"id": "cat-groceries", "name": "Groceries", "hidden": False, "deleted": False},
                    ],
                },
                {
                    "name": "Credit Card Payments",
                    "categories": [
                        {"id": "cat-ccpayment", "name": "Amazon Prime Store Card", "hidden": False, "deleted": False},
                    ],
                },
            ]
        }
    }

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(categories.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(settings, "ynab_personal_access_token", "fake-token")
    monkeypatch.setattr(settings, "ynab_budget_id", "budget-1")


def test_get_ynab_categories_excludes_internal_hidden_deleted_and_cc_payments(mock_categories_response):
    result = categories.get_ynab_categories()
    ids = {c.category_id for c in result}

    assert ids == {"cat-pet", "cat-groceries"}


def test_get_ynab_categories_includes_categories_from_every_real_group(mock_categories_response):
    """Regression test for the removed [Auto]-prefix gate: categories from any
    normal group must be offered, not just a specially-named opt-in group."""
    result = categories.get_ynab_categories()
    groups = {c.group for c in result}

    assert groups == {"Household", "Everyday Expenses"}
