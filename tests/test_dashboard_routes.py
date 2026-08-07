import json
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.dashboard import routes as routes_module
from app.dashboard.routes import router
from app.models import Item, Receipt
from app.ynab import apply as apply_module

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Mounts the router on a fresh FastAPI instance rather than importing
    app.main, which starts the scheduler (per the handoff's own note)."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return TestClient(app)


def _seed(order_id, match_status, order_date="2026-01-01", grand_total="10.00", txn_id="txn-1"):
    db.insert_scraped_order(order_id, html_path="tests/fixtures/sample_receipt.html")
    receipt = Receipt(
        grand_total=Decimal(grand_total), subtotal=Decimal(grand_total), total_before_tax=Decimal(grand_total),
        date=order_date, items=[Item(price=Decimal(grand_total), title="Widget", short_name="Widget", category="other")],
    )
    db.update_parsed(order_id, receipt)
    if match_status == "pending_review":
        db.set_match_result(
            order_id, "pending_review", selected_txn_id=txn_id,
            patch_payload_json=json.dumps({"memo": f"Widget ({order_id})", "category_id": None}),
            candidate_ids_json=json.dumps([{"id": txn_id, "date": order_date, "amount": -1000, "payee": "Amazon"}]),
        )
    elif match_status == "ambiguous":
        db.set_match_result(
            order_id, "ambiguous",
            candidate_ids_json=json.dumps([
                {"id": "txn-a", "date": order_date, "amount": -1000, "payee": "Amazon"},
                {"id": "txn-b", "date": order_date, "amount": -1000, "payee": "Amazon"},
            ]),
        )
    elif match_status != "pending_parse":
        db.set_match_result(order_id, match_status)
    return order_id


# --- GET routes: reachability ---

@pytest.mark.parametrize("path", ["/", "/review", "/history", "/logs", "/logs?lines=10"])
def test_get_routes_return_200_on_empty_db(client, path):
    assert client.get(path).status_code == 200


def test_get_routes_return_200_with_one_order_per_status(client):
    for status in ["pending_review", "ambiguous", "no_candidate", "approved", "error", "rejected"]:
        _seed(f"ORDER-{status.upper()}", status, txn_id=f"txn-{status}")

    db.insert_scraped_order("PARSE-ERR", html_path="tests/fixtures/sample_receipt.html")
    db.mark_parse_error("PARSE-ERR", "LLM returned invalid JSON")

    for path in ["/", "/review", "/history", "/history?status=approved", "/history?status=error", "/logs"]:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"

    for order_id in [
        "ORDER-PENDING_REVIEW", "ORDER-AMBIGUOUS", "ORDER-NO_CANDIDATE",
        "ORDER-APPROVED", "ORDER-ERROR", "ORDER-REJECTED", "PARSE-ERR",
    ]:
        resp = client.get(f"/receipts/{order_id}")
        assert resp.status_code == 200, f"receipt detail {order_id} -> {resp.status_code}"


def test_receipt_html_route_sandboxes_untrusted_content(client):
    _seed("ORDER-1", "pending_review")
    resp = client.get("/receipts/ORDER-1/html")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == "sandbox"


def test_receipt_detail_404s_on_unknown_order(client):
    assert client.get("/receipts/DOES-NOT-EXIST").status_code == 404


def test_history_status_filter_scopes_results(client):
    _seed("A", "approved")
    _seed("B", "no_candidate")

    resp = client.get("/history?status=approved")
    assert resp.status_code == 200
    assert "A" in resp.text
    assert "B" not in resp.text


# --- POST routes ---

def test_approve_applies_and_redirects_with_flash(client, monkeypatch):
    _seed("ORDER-1", "pending_review", txn_id="txn-x")
    monkeypatch.setattr(apply_module.ynab_client, "get_transaction", lambda tid: {"id": tid, "deleted": False, "amount": -10000, "cleared": "uncleared"})
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: {"id": tid})

    resp = client.post("/orders/ORDER-1/approve", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/review?")
    assert db.get_order("ORDER-1")["match_status"] == "approved"


def test_reject_works_from_pending_review_not_from_approved(client):
    _seed("ORDER-1", "pending_review")
    _seed("ORDER-2", "approved", txn_id="txn-2")

    resp1 = client.post("/orders/ORDER-1/reject", follow_redirects=False)
    assert resp1.status_code == 303
    assert db.get_order("ORDER-1")["match_status"] == "rejected"

    resp2 = client.post("/orders/ORDER-2/reject", follow_redirects=False)
    assert resp2.status_code == 303
    assert db.get_order("ORDER-2")["match_status"] == "approved"  # unchanged


def test_pick_candidate_demotes_ambiguous_to_pending_review(client, monkeypatch):
    _seed("ORDER-1", "ambiguous")
    monkeypatch.setattr(settings, "ynab_personal_access_token", "")  # skip real category lookup

    resp = client.post("/orders/ORDER-1/pick-candidate", data={"txn_id": "txn-a"}, follow_redirects=False)
    assert resp.status_code == 303

    row = db.get_order("ORDER-1")
    assert row["match_status"] == "pending_review"
    assert row["selected_ynab_txn_id"] == "txn-a"


def test_create_transaction_route_disabled_by_default(client, monkeypatch):
    _seed("ORDER-1", "no_candidate")
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "post_transaction", lambda payload: calls.append(payload))

    resp = client.post(
        "/orders/ORDER-1/create-transaction", data={"confirm": "on"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert len(calls) == 0
    assert db.get_order("ORDER-1")["match_status"] == "no_candidate"


def test_create_transaction_route_works_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "ynab_allow_create_without_match", True)
    monkeypatch.setattr(settings, "ynab_account_id", "acct-1")
    _seed("ORDER-1", "no_candidate")
    monkeypatch.setattr(apply_module.ynab_client, "post_transaction", lambda payload: {"id": "new-txn"})

    resp = client.post(
        "/orders/ORDER-1/create-transaction", data={"confirm": "on"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.get_order("ORDER-1")["match_status"] == "approved"


def test_reset_route_disabled_by_default(client):
    _seed("ORDER-1", "error")
    resp = client.post("/orders/ORDER-1/reset", data={"target": "pending_review"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_order("ORDER-1")["match_status"] == "error"  # untouched


def test_reapply_route_disabled_by_default(client, monkeypatch):
    _seed("ORDER-1", "approved", txn_id="txn-x")
    calls = []
    monkeypatch.setattr(apply_module.ynab_client, "patch_transaction", lambda tid, payload: calls.append(tid))

    resp = client.post("/orders/ORDER-1/reapply", data={"confirm": "on"}, follow_redirects=False)
    assert resp.status_code == 303
    assert len(calls) == 0


def test_run_now_spawns_pipeline_without_calling_real_scraper(client, monkeypatch):
    """Must never let a dashboard test reach the real scraper/LLM/YNAB — stub
    the pipeline function the route calls and just prove it's invoked."""
    called = threading.Event()
    monkeypatch.setattr(routes_module, "run_pipeline", lambda: called.set())

    resp = client.post("/run-now", follow_redirects=False)
    assert resp.status_code == 303

    assert called.wait(timeout=2), "run_pipeline stub was never invoked"
