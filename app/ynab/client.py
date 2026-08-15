import logging
import time

import requests

from app.config import settings

logger = logging.getLogger(__name__)

# (connect timeout, read timeout). Without this, one hung TCP connection wedges
# the pipeline thread indefinitely — with the scheduler's max_instances=1, that
# silently skips every future scheduled run too (docs/IMPROVEMENTS.md item 2).
_TIMEOUT = (5, 30)

# Retry only on 429 -- a 429 is a pre-processing rejection (YNAB never
# touched the write), so it's always safe to retry, unlike a generic 5xx or
# timeout where the server might already have applied the change. Three real
# approvals were burned by 429s during the apply-time re-fetch in one sitting
# before this existed, parking those orders in terminal 'error' for 8 days
# (docs/IMPROVEMENTS.md 5.3).
_MAX_ATTEMPTS = 3
_MAX_RETRY_SLEEP_SECONDS = 30


def _retry_sleep_seconds(response: requests.Response) -> float:
    """Honors Retry-After when YNAB sends one, capped at ~30s either way so a
    misbehaving/huge Retry-After value can't stall the pipeline thread for
    longer than that."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return _MAX_RETRY_SLEEP_SECONDS
    try:
        seconds = float(retry_after)
    except ValueError:
        # Retry-After can technically be an HTTP-date instead of a delta --
        # not worth parsing for a cap this small, fall back to the max cap.
        return _MAX_RETRY_SLEEP_SECONDS
    return min(max(seconds, 0.0), _MAX_RETRY_SLEEP_SECONDS)


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Every YNAB call (GET/PATCH/POST alike) goes through this. Any status
    other than 429 behaves exactly as before this existed: raise_for_status()
    on the first response, no retry. A 429 gets up to _MAX_ATTEMPTS tries
    total, honoring Retry-After between them."""
    response = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = requests.request(method, url, timeout=_TIMEOUT, **kwargs)
        if response.status_code == 429 and attempt < _MAX_ATTEMPTS:
            sleep_for = _retry_sleep_seconds(response)
            logger.warning(
                "YNAB %s %s rate-limited (429) -- retrying in %.1fs (attempt %d/%d)",
                method, url, sleep_for, attempt, _MAX_ATTEMPTS,
            )
            time.sleep(sleep_for)
            continue
        break
    response.raise_for_status()
    return response


def _headers() -> dict:
    return {
        "accept": "application/json",
        "Authorization": f"Bearer {settings.ynab_personal_access_token}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return f"https://api.ynab.com/v1/budgets/{settings.ynab_budget_id}"


def get_budgets() -> list[dict]:
    """Lists every budget this token can see. Use this to find an explicit
    YNAB_BUDGET_ID rather than relying on "last-used", which silently follows
    whichever budget was most recently opened in the YNAB app/web UI and can
    point somewhere unintended (this is what caused the wrong-budget mixup)."""
    resp = _request("GET", "https://api.ynab.com/v1/budgets", headers=_headers())
    return resp.json()["data"]["budgets"]


def get_accounts(budget_id: str | None = None) -> list[dict]:
    """Reused from vendor/ynab_amazon/ynab.py's get_accounts — used for setup (finding
    the right YNAB_ACCOUNT_ID) and as a lightweight connectivity/token check.
    Accepts an explicit budget_id override so callers can inspect a budget other
    than the one currently configured in settings."""
    bid = budget_id or settings.ynab_budget_id
    resp = _request("GET", f"https://api.ynab.com/v1/budgets/{bid}/accounts", headers=_headers())
    return resp.json()["data"]["accounts"]


def get_transaction(transaction_id: str) -> dict:
    resp = _request("GET", f"{_base_url()}/transactions/{transaction_id}", headers=_headers())
    return resp.json()["data"]["transaction"]


def get_transactions_since(account_id: str, since_date: str) -> list[dict]:
    resp = _request(
        "GET",
        f"{_base_url()}/accounts/{account_id}/transactions",
        headers=_headers(),
        params={"since_date": since_date},
    )
    return resp.json()["data"]["transactions"]


def patch_transaction(transaction_id: str, payload: dict) -> dict:
    resp = _request(
        "PATCH",
        f"{_base_url()}/transactions/{transaction_id}",
        headers=_headers(),
        json={"transaction": payload},
    )
    return resp.json()["data"]["transaction"]


def post_transaction(payload: dict) -> dict:
    """Creates a brand-new transaction. Only used for the no-bank-match backfill
    path (app/ynab/apply.py:create_transaction) — the normal flow only ever
    PATCHes an existing bank-fed transaction, never creates one."""
    resp = _request(
        "POST",
        f"{_base_url()}/transactions",
        headers=_headers(),
        json={"transaction": payload},
    )
    return resp.json()["data"]["transaction"]
