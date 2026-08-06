import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

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
