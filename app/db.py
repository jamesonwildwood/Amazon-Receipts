import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models import Receipt

SCHEMA = """
CREATE TABLE IF NOT EXISTS amazon_orders (
    order_id TEXT PRIMARY KEY,
    order_date DATE,
    html_path TEXT NOT NULL,
    scraped_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    parsed_json TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(parse_status IN ('pending','parsed','error')),
    parse_error TEXT,
    parsed_at DATETIME,
    grand_total_cents INTEGER,

    match_status TEXT NOT NULL DEFAULT 'pending_parse'
        CHECK(match_status IN
          ('pending_parse','pending_review','applying','approved',
           'no_candidate','ambiguous','error','rejected')),
    candidate_ynab_txn_ids TEXT,
    selected_ynab_txn_id TEXT,
    ynab_patch_payload TEXT,
    matched_at DATETIME,

    approved_at DATETIME,
    ynab_transaction_id_patched TEXT,
    ynab_patched_at DATETIME,
    ynab_patch_error TEXT,
    apply_count INTEGER NOT NULL DEFAULT 0,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ynab_apply_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES amazon_orders(order_id),
    ynab_transaction_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    is_reapply BOOLEAN NOT NULL DEFAULT 0,
    success BOOLEAN NOT NULL,
    error_message TEXT,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','success','partial','error')),
    orders_found INTEGER DEFAULT 0,
    orders_parsed INTEGER DEFAULT 0,
    orders_matched INTEGER DEFAULT 0,
    error_message TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def has_order(order_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM amazon_orders WHERE order_id = ?", (order_id,)).fetchone()
        return row is not None


def insert_scraped_order(order_id: str, html_path: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO amazon_orders (order_id, html_path) VALUES (?, ?)",
            (order_id, html_path),
        )
        conn.commit()


# --- reads -------------------------------------------------------------

def get_order(order_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM amazon_orders WHERE order_id = ?", (order_id,)
        ).fetchone()


def list_orders(match_status: Optional[str] = None) -> list[sqlite3.Row]:
    # order by rowid too: created_at (CURRENT_TIMESTAMP) has second-level resolution
    # and many orders can be scraped within the same second.
    with connect() as conn:
        if match_status is None:
            return conn.execute(
                "SELECT * FROM amazon_orders ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM amazon_orders WHERE match_status = ? ORDER BY created_at DESC, rowid DESC",
            (match_status,),
        ).fetchall()


def list_orders_by_statuses(statuses: tuple[str, ...]) -> list[sqlite3.Row]:
    """Like list_orders(), but for the Home page's needs-attention queue,
    which spans several statuses at once (pending_review + ambiguous +
    error) rather than one."""
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM amazon_orders WHERE match_status IN ({placeholders}) "
            "ORDER BY created_at DESC, rowid DESC",
            statuses,
        ).fetchall()


def list_parse_error_orders() -> list[sqlite3.Row]:
    """Orders where parsing itself failed (parse_status='error') — distinct
    from match_status='error', which is an apply/matching failure. Both need
    to show up in the same needs-attention queue."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM amazon_orders WHERE parse_status = 'error' ORDER BY created_at DESC, rowid DESC"
        ).fetchall()


def mark_rejected(order_id: str) -> bool:
    """Dismisses a wrong match — the only way to leave pending_review/ambiguous
    without either approving or letting the matcher pick again. Guarded: only
    valid from those two statuses, so it can't silently overwrite an already-
    approved order's state."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE amazon_orders SET match_status = 'rejected', updated_at = CURRENT_TIMESTAMP "
            "WHERE order_id = ? AND match_status IN ('pending_review', 'ambiguous')",
            (order_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def list_pending_parse_order_ids() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT order_id FROM amazon_orders WHERE parse_status = 'pending'"
        ).fetchall()
        return [r["order_id"] for r in rows]


def list_pending_match_order_ids() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT order_id FROM amazon_orders "
            "WHERE parse_status = 'parsed' AND match_status = 'pending_parse'"
        ).fetchall()
        return [r["order_id"] for r in rows]


def list_no_candidate_order_ids_since(min_date: str) -> list[str]:
    """Orders stuck at no_candidate whose order_date is recent enough that the
    bank feed might have caught up since the last attempt (Amazon typically
    charges at shipment, 1-15+ days after ordering). Bounded by min_date so
    ancient orders — where the account genuinely has no data for that period —
    aren't re-fetched forever."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT order_id FROM amazon_orders "
            "WHERE match_status = 'no_candidate' AND order_date >= ?",
            (min_date,),
        ).fetchall()
        return [r["order_id"] for r in rows]


def bound_transaction_ids() -> set[str]:
    """Transaction ids already applied to an approved order — the matcher must
    never offer these as candidates for a different order."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT ynab_transaction_id_patched FROM amazon_orders "
            "WHERE match_status = 'approved' AND ynab_transaction_id_patched IS NOT NULL"
        ).fetchall()
        return {r["ynab_transaction_id_patched"] for r in rows}


def find_order_bound_to(txn_id: str, exclude_order_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM amazon_orders WHERE ynab_transaction_id_patched = ? "
            "AND order_id != ? AND match_status = 'approved'",
            (txn_id, exclude_order_id),
        ).fetchone()


def count_by_match_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT match_status, COUNT(*) as n FROM amazon_orders GROUP BY match_status"
        ).fetchall()
        return {r["match_status"]: r["n"] for r in rows}


def count_reapplied() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM amazon_orders WHERE apply_count > 1").fetchone()
        return row["n"]


def get_apply_log(order_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        # order by id, not applied_at: CURRENT_TIMESTAMP has second-level resolution,
        # so two rapid inserts (e.g. an apply immediately followed by a re-apply in
        # a test or a fast manual retry) can tie and applied_at DESC alone would be
        # non-deterministic. id is monotonic with insertion order.
        return conn.execute(
            "SELECT * FROM ynab_apply_log WHERE order_id = ? ORDER BY id DESC",
            (order_id,),
        ).fetchall()


def get_last_run() -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()


def list_runs(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# --- parsing writes ------------------------------------------------------

def update_parsed(order_id: str, receipt: Receipt) -> None:
    """Records a successful parse and resets match-related state so the order is
    ready to be matched fresh. Deliberately leaves ynab_transaction_id_patched /
    ynab_patched_at / apply_count / the ynab_apply_log history untouched, even when
    this is called from a dev reset-and-reparse — that history stays visible."""
    grand_total_cents = int(round(receipt.grand_total * 100))
    with connect() as conn:
        conn.execute(
            "UPDATE amazon_orders SET "
            "parsed_json = ?, parse_status = 'parsed', parse_error = NULL, "
            "parsed_at = CURRENT_TIMESTAMP, grand_total_cents = ?, order_date = ?, "
            "match_status = 'pending_parse', candidate_ynab_txn_ids = NULL, "
            "selected_ynab_txn_id = NULL, ynab_patch_payload = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE order_id = ?",
            (receipt.model_dump_json(), grand_total_cents, receipt.date.isoformat(), order_id),
        )
        conn.commit()


def mark_parse_error(order_id: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE amazon_orders SET parse_status = 'error', parse_error = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (error, order_id),
        )
        conn.commit()


# --- matching writes -----------------------------------------------------

def set_match_result(
    order_id: str,
    match_status: str,
    selected_txn_id: Optional[str] = None,
    patch_payload_json: Optional[str] = None,
    candidate_ids_json: Optional[str] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE amazon_orders SET match_status = ?, selected_ynab_txn_id = ?, "
            "ynab_patch_payload = ?, candidate_ynab_txn_ids = ?, matched_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (match_status, selected_txn_id, patch_payload_json, candidate_ids_json, order_id),
        )
        conn.commit()


# --- apply writes (see app/ynab/apply.py for the guarded routine that calls these) --

def claim_for_apply(order_id: str, allowed_from: tuple[str, ...]) -> bool:
    """Atomic UPDATE...WHERE claim. Returns True iff this call was the one that
    moved the row into 'applying' — the mechanism that makes Approve idempotent
    against a double-click or an overlapping scheduler run."""
    placeholders = ",".join("?" for _ in allowed_from)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE amazon_orders SET match_status = 'applying', updated_at = CURRENT_TIMESTAMP "
            f"WHERE order_id = ? AND match_status IN ({placeholders})",
            (order_id, *allowed_from),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_approved(order_id: str, txn_id: str, payload_json: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE amazon_orders SET match_status = 'approved', "
            "ynab_transaction_id_patched = ?, ynab_patched_at = CURRENT_TIMESTAMP, "
            "ynab_patch_payload = ?, ynab_patch_error = NULL, apply_count = apply_count + 1, "
            "approved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE order_id = ?",
            (txn_id, payload_json, order_id),
        )
        conn.commit()


def mark_error(order_id: str, error_message: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE amazon_orders SET match_status = 'error', ynab_patch_error = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (error_message, order_id),
        )
        conn.commit()


def log_apply_attempt(
    order_id: str,
    txn_id: str,
    payload_json: str,
    is_reapply: bool,
    success: bool,
    error_message: Optional[str] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO ynab_apply_log "
            "(order_id, ynab_transaction_id, payload_json, is_reapply, success, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, txn_id, payload_json, is_reapply, success, error_message),
        )
        conn.commit()


# --- pipeline run tracking ------------------------------------------------

def mark_stale_runs_as_error(older_than_hours: int) -> int:
    """Marks any leftover 'running' pipeline_runs rows older than the given
    threshold as 'error' — e.g. a crash mid-run leaves a row stuck at
    'running' forever, which today permanently disables Run Now in the
    dashboard (it thinks a run is still in progress). Call at app startup.
    Returns the number of rows fixed."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE pipeline_runs SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
            "error_message = 'marked stale at startup (crashed mid-run)' "
            "WHERE status = 'running' AND started_at < datetime('now', ?)",
            (f"-{older_than_hours} hours",),
        )
        conn.commit()
        return cur.rowcount


def start_run() -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (started_at, status) VALUES (CURRENT_TIMESTAMP, 'running')"
        )
        conn.commit()
        return cur.lastrowid


def finish_run(
    run_id: int,
    status: str,
    orders_found: int,
    orders_parsed: int,
    orders_matched: int,
    error_message: Optional[str] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET finished_at = CURRENT_TIMESTAMP, status = ?, "
            "orders_found = ?, orders_parsed = ?, orders_matched = ?, error_message = ? "
            "WHERE id = ?",
            (status, orders_found, orders_parsed, orders_matched, error_message, run_id),
        )
        conn.commit()
