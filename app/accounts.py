"""Loads the configured Amazon accounts to scrape (docs/IMPROVEMENTS.md 3.1).
Each account is one Amazon login; all of them share this app's single SQLite
claim ledger (docs/IMPROVEMENTS.md Part 3 intro) because both can charge the
same card, so keeping them in one process/DB is a deliberate requirement, not
just a convenience.

Deliberately NOT editable from the dashboard: credentials live only in this
toml file (or the legacy env vars), never in SQLite/backups. Restart the
process to pick up toml edits -- see docs/IMPROVEMENTS.md 3.5.
"""

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Labels are stored on orders and used as a Chrome-profile subdirectory name,
# so they must be non-empty and safe to drop straight into a filesystem path.
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class AmazonAccount:
    label: str
    email: str
    password: str
    totp_secret: str = ""
    # Optional per-account override -- omit in the toml to use the global
    # YNAB_ACCOUNT_ID. Both accounts charging the same card is the expected
    # setup today; this field exists so a separate-card household doesn't
    # need a code change (docs/IMPROVEMENTS.md 3.4).
    ynab_account_id: Optional[str] = None


def _validate_account(raw: dict, index: int, source: Path) -> AmazonAccount:
    label = raw.get("label")
    if not label or not isinstance(label, str):
        raise ValueError(f"{source}: account #{index + 1} is missing a non-empty 'label'")
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"{source}: label {label!r} must be filesystem-safe "
            "(letters, digits, '-', '_' only -- it's used in Chrome-profile paths)"
        )
    email = raw.get("email")
    password = raw.get("password")
    if not email:
        raise ValueError(f"{source}: account '{label}' is missing 'email'")
    if not password:
        raise ValueError(f"{source}: account '{label}' is missing 'password'")
    return AmazonAccount(
        label=label,
        email=email,
        password=password,
        totp_secret=raw.get("totp_secret") or "",
        ynab_account_id=raw.get("ynab_account_id") or None,
    )


def _load_toml_accounts(path: Path) -> list[AmazonAccount]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    raw_accounts = data.get("accounts", [])
    if not raw_accounts:
        raise ValueError(f"{path}: no [[accounts]] entries found")

    accounts = [_validate_account(raw, i, path) for i, raw in enumerate(raw_accounts)]

    labels = [a.label for a in accounts]
    dupes = sorted({label for label in labels if labels.count(label) > 1})
    if dupes:
        raise ValueError(f"{path}: duplicate account label(s): {', '.join(dupes)}")
    return accounts


def _load_env_account() -> Optional[AmazonAccount]:
    """Back-compat: pre-multi-account deployments configured one Amazon login
    via AMAZON_EMAIL/AMAZON_PASSWORD/AMAZON_TOTP_SECRET. Synthesize it as a
    one-account list labeled 'default' so those deployments keep working
    unmodified (docs/IMPROVEMENTS.md 3.1)."""
    if not (settings.amazon_email and settings.amazon_password):
        return None
    return AmazonAccount(
        label="default",
        email=settings.amazon_email,
        password=settings.amazon_password,
        totp_secret=settings.amazon_totp_secret,
    )


def load_accounts() -> list[AmazonAccount]:
    """Loads the account list fresh every call -- deliberately uncached. The
    toml is small and this is read at most once per pipeline run or dashboard
    request; "restart to reload" (module docstring) is a UI/editability
    decision, not a reason to cache a cheap file read.

    Returns [] if neither the toml nor the legacy env vars are configured --
    callers must treat that as "nothing to scrape," not an error."""
    path = Path(settings.amazon_accounts_path)
    toml_accounts = _load_toml_accounts(path) if path.exists() else None
    env_account = _load_env_account()

    if toml_accounts is not None:
        if env_account is not None:
            logger.warning(
                "Both %s and AMAZON_EMAIL/AMAZON_PASSWORD are set -- %s wins. "
                "Remove the env vars (or the toml) to silence this warning.",
                path,
                path,
            )
        return toml_accounts

    if env_account is not None:
        return [env_account]

    return []


def ynab_account_id_for_label(label: str, accounts: Optional[list["AmazonAccount"]] = None) -> str:
    """Resolves an order's Amazon account -> its ynab_account_id override,
    else the global YNAB_ACCOUNT_ID (docs/IMPROVEMENTS.md 3.4). Falls back to
    the global id for an unrecognized label too (e.g. an account removed from
    the toml after it scraped historical orders) rather than raising."""
    accounts = accounts if accounts is not None else load_accounts()
    for account in accounts:
        if account.label == label:
            return account.ynab_account_id or settings.ynab_account_id
    return settings.ynab_account_id
