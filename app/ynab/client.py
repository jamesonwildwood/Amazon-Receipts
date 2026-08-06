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
