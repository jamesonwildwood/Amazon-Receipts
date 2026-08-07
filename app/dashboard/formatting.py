"""Jinja formatting helpers: status badges, money/time formatting, and a
mapping from ApplyResult.reason codes to human-readable flash messages.
Kept separate from routes.py since templates need these but routes.py's
job is request handling, not presentation formatting."""

import datetime as dt

# match_status -> (label, tone). Tone drives CSS class only — label always
# carries the meaning too, per docs/IMPROVEMENTS.md ("never color alone").
MATCH_STATUS_BADGES = {
    "pending_parse": ("Awaiting parse", "neutral"),
    "pending_review": ("Needs review", "amber"),
    "applying": ("Applying…", "neutral"),
    "approved": ("Applied", "green"),
    "no_candidate": ("No bank match", "neutral"),
    "ambiguous": ("Ambiguous", "orange"),
    "error": ("Error", "red"),
    "rejected": ("Rejected", "muted"),
}

PARSE_STATUS_BADGES = {
    "pending": ("Awaiting parse", "neutral"),
    "parsed": ("Parsed", "green"),
    "error": ("Parse error", "red"),
}

RUN_STATUS_BADGES = {
    "running": ("Running", "neutral"),
    "success": ("Success", "green"),
    "partial": ("Partial", "amber"),
    "error": ("Error", "red"),
}

# ApplyResult.reason -> (human message, tone). Every apply.py ApplyResult.reason
# value must have an entry — a redirect with an unmapped reason falls back to a
# generic message rather than silently showing nothing (today a failed Approve
# is indistinguishable from a successful one, per docs/IMPROVEMENTS.md).
APPLY_REASON_MESSAGES = {
    "applied": ("Applied — the YNAB transaction was updated.", "success"),
    "created": ("Created a new YNAB transaction.", "success"),
    "already_applied": ("Already applied — nothing to do.", "neutral"),
    "already_processing": ("Already being processed by another request — try again shortly.", "amber"),
    "nothing_staged": ("Nothing staged to apply — try re-matching this order.", "error"),
    "refetch_failed": ("Couldn't re-check the transaction with YNAB — will need a retry.", "error"),
    "transaction_deleted": ("That transaction was deleted in YNAB since matching — can't apply.", "error"),
    "amount_changed": ("That transaction's amount changed in YNAB since matching — can't apply.", "error"),
    "transaction_reconciled": ("That transaction was reconciled in YNAB since matching — can't apply.", "error"),
    "transaction_already_claimed": ("That transaction is already claimed by a different order.", "error"),
    "patch_failed": ("YNAB rejected the update — see the order's error message.", "error"),
    "create_failed": ("YNAB rejected the new transaction — see the order's error message.", "error"),
    "reapply_disabled": ("Force Re-Apply is disabled (set ALLOW_REAPPLY=true to enable).", "amber"),
    "create_without_match_disabled": (
        "Creating without a match is disabled (set YNAB_ALLOW_CREATE_WITHOUT_MATCH=true to enable).",
        "amber",
    ),
    "order_not_found": ("Order not found.", "error"),
    "rejected": ("Rejected — dismissed from the review queue.", "neutral"),
    "reject_failed": ("Couldn't reject — order isn't pending review or ambiguous anymore.", "error"),
    "reset_applied": ("Reset — re-matching will run again.", "success"),
    "reset_disabled": ("Reset is disabled (set ALLOW_RESET=true to enable).", "amber"),
}


def badge(status: str, kind: str = "match") -> tuple[str, str]:
    table = {"match": MATCH_STATUS_BADGES, "parse": PARSE_STATUS_BADGES, "run": RUN_STATUS_BADGES}[kind]
    return table.get(status, (status, "neutral"))


def apply_reason_message(reason: str | None) -> tuple[str, str]:
    if not reason:
        return ("Done.", "neutral")
    return APPLY_REASON_MESSAGES.get(reason, (reason, "neutral"))


def format_cents(cents) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:,.2f}"


def format_milliunits(milli) -> str:
    if milli is None:
        return "—"
    return f"${milli / 1000:,.2f}"


def format_reltime(iso_timestamp: str | None) -> str:
    """SQLite CURRENT_TIMESTAMP values are UTC but carry no timezone marker —
    rendered as-is today, which reads as local time (docs/IMPROVEMENTS.md).
    Assume UTC, compare against real UTC now, render as relative + note it's UTC."""
    if not iso_timestamp:
        return "never"
    try:
        then = dt.datetime.fromisoformat(iso_timestamp).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return iso_timestamp
    delta = dt.datetime.now(dt.timezone.utc) - then
    seconds = delta.total_seconds()
    if seconds < 0:
        return iso_timestamp
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def truncate_id(value: str | None, length: int = 8) -> str:
    if not value:
        return "—"
    return value[:length]


def mask_email(email: str | None) -> str:
    """Account credentials are never rendered in the UI, masked or not
    (docs/IMPROVEMENTS.md 3.5) -- this masks even the domain (unlike a
    typical "j***@gmail.com" mask) so the integration-health card's per-
    account row identifies *which* configured account without leaking any
    part of its email beyond the first character."""
    if not email:
        return "—"
    return f"{email[0]}***@…"
