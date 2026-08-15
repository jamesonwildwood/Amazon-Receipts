"""Config sanity checks (docs/IMPROVEMENTS.md 5.5).

The production server ran for eight straight nights with a revoked YNAB
token and nobody knew — a pull-based dashboard can't surface anything to
someone who doesn't open it. These checks run once at scheduler startup and
once at the start of every pipeline run (never on a dashboard GET request —
that would fire a notification on every page load, which is a side effect a
read must never have). Results are recorded in the config_health table
(surfaced as a Home banner, app/dashboard/routes.py) and a 5.1 notification
fires the moment a check starts failing.

LLM-key sanity is deliberately NOT checked here: there's no cheap
authenticated call for either the Anthropic or OpenAI-compatible provider
that doesn't either spend tokens or require shaping a request like a real
extraction call, so it's skipped rather than faked — see the PR description.
"""

import logging

from app import db, notify
from app.accounts import load_accounts
from app.config import settings
from app.ynab import client as ynab_client

logger = logging.getLogger(__name__)


def _http_status_code(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def check_ynab_config() -> None:
    """One cheap authenticated call (get_accounts) catches exactly the two
    "wrong setting, not a real outage" failures that bit in production: a
    401 means YNAB_PERSONAL_ACCESS_TOKEN is bad/revoked; a 404 means
    YNAB_BUDGET_ID is wrong or stale (the "last-used" trap docs/DESIGN.md
    warns about). Any other error (network blip, 429, 5xx) is not a config
    problem and is left alone — the pipeline's own retry/error handling
    covers that.

    Skipped entirely when no token is configured yet: that's an unfinished
    setup, not a "failure" worth banner-ing and emailing about."""
    if not settings.ynab_personal_access_token:
        return

    try:
        ynab_client.get_accounts()
    except Exception as exc:
        status_code = _http_status_code(exc)
        if status_code == 401:
            message = "YNAB_PERSONAL_ACCESS_TOKEN looks invalid or revoked (401 from a routine accounts check)"
        elif status_code == 404:
            message = "YNAB_BUDGET_ID looks wrong or stale (404 from a routine accounts check)"
        else:
            logger.warning("YNAB config sanity check hit a non-config error (leaving prior status as-is): %s", exc)
            return
        logger.error(message)
        db.record_config_health("ynab", False, message)
        notify.send_email(
            "Amazon Receipts: YNAB config problem",
            f"{message}\n\nDashboard: {settings.notify_dashboard_url}",
        )
        return

    db.record_config_health("ynab", True, None)


def check_amazon_accounts_config() -> None:
    """The Home route already calls load_accounts() per request to show an
    inline error (app/dashboard/routes.py home()) — that's a plain read with
    no side effects and stays exactly as-is. This is the periodic copy that
    additionally records + notifies, so a broken amazon_accounts.toml
    doesn't silently drop orders for days without anyone finding out (the
    same class of bug this whole section exists for)."""
    try:
        load_accounts()
    except Exception as exc:
        message = f"amazon_accounts.toml is invalid: {exc}"
        logger.error(message)
        db.record_config_health("amazon_accounts", False, message)
        notify.send_email(
            "Amazon Receipts: amazon_accounts.toml problem",
            f"{message}\n\nDashboard: {settings.notify_dashboard_url}",
        )
        return

    db.record_config_health("amazon_accounts", True, None)


def run_startup_checks() -> None:
    """Called once at scheduler startup (app/scheduler.py) and once at the
    start of every pipeline run (app/pipeline.py, which also covers
    `python -m app run`)."""
    check_ynab_config()
    check_amazon_accounts_config()
