import requests

from app.config import settings


def _headers() -> dict:
    return {
        "accept": "application/json",
        "Authorization": f"Bearer {settings.ynab_personal_access_token}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return f"https://api.ynab.com/v1/budgets/{settings.ynab_budget_id}"


def get_accounts() -> list[dict]:
    """Reused from vendor/ynab_amazon/ynab.py's get_accounts — used for setup (finding
    the right YNAB_ACCOUNT_ID) and as a lightweight connectivity/token check."""
    resp = requests.get(f"{_base_url()}/accounts", headers=_headers())
    resp.raise_for_status()
    return resp.json()["data"]["accounts"]


def get_transaction(transaction_id: str) -> dict:
    resp = requests.get(f"{_base_url()}/transactions/{transaction_id}", headers=_headers())
    resp.raise_for_status()
    return resp.json()["data"]["transaction"]


def get_transactions_since(account_id: str, since_date: str) -> list[dict]:
    resp = requests.get(
        f"{_base_url()}/accounts/{account_id}/transactions",
        headers=_headers(),
        params={"since_date": since_date},
    )
    resp.raise_for_status()
    return resp.json()["data"]["transactions"]


def patch_transaction(transaction_id: str, payload: dict) -> dict:
    resp = requests.patch(
        f"{_base_url()}/transactions/{transaction_id}",
        headers=_headers(),
        json={"transaction": payload},
    )
    resp.raise_for_status()
    return resp.json()["data"]["transaction"]


def post_transaction(payload: dict) -> dict:
    """Creates a brand-new transaction. Only used for the no-bank-match backfill
    path (app/ynab/apply.py:create_transaction) — the normal flow only ever
    PATCHes an existing bank-fed transaction, never creates one."""
    resp = requests.post(
        f"{_base_url()}/transactions",
        headers=_headers(),
        json={"transaction": payload},
    )
    resp.raise_for_status()
    return resp.json()["data"]["transaction"]
