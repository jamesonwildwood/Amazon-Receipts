import json
import threading
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import datetime as dt

from app import db
from app.config import settings
from app.pipeline import STALE_RUN_AFTER_HOURS, run_pipeline
from app.scheduler import get_next_run_time
from app.ynab.apply import apply_patch, reset_order
from app.ynab.matcher import pick_candidate

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _pretty(json_text: str | None) -> str:
    if not json_text:
        return "—"
    try:
        return json.dumps(json.loads(json_text), indent=2)
    except (TypeError, ValueError):
        return json_text


def _run_in_progress(last_run) -> bool:
    """A 'running' row older than STALE_RUN_AFTER_HOURS is treated as crashed,
    not in-progress — otherwise Run Now stays disabled forever after a hang
    (docs/IMPROVEMENTS.md item 3). The startup sweep (app/main.py) fixes this
    in the DB after a process restart; this covers the same-process case too
    (a long-hung thread without a crash)."""
    if not last_run or last_run["status"] != "running":
        return False
    started_at = dt.datetime.fromisoformat(last_run["started_at"])
    return (dt.datetime.utcnow() - started_at) < dt.timedelta(hours=STALE_RUN_AFTER_HOURS)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    last_run = db.get_last_run()
    counts = db.count_by_match_status()
    with db.connect() as conn:
        last_scrape_at = conn.execute("SELECT MAX(scraped_at) as m FROM amazon_orders").fetchone()["m"]
        last_ynab_success_at = conn.execute(
            "SELECT MAX(applied_at) as m FROM ynab_apply_log WHERE success = 1"
        ).fetchone()["m"]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "last_run": last_run,
            "run_in_progress": _run_in_progress(last_run),
            "next_run_time": get_next_run_time(),
            "counts": counts,
            "reapplied_count": db.count_reapplied(),
            "last_scrape_at": last_scrape_at,
            "last_ynab_success_at": last_ynab_success_at,
        },
    )


@router.post("/run-now")
def run_now():
    threading.Thread(target=run_pipeline, daemon=True).start()
    return RedirectResponse(url="/", status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review(request: Request):
    pending = []
    for row in db.list_orders("pending_review"):
        pending.append(
            {
                "order_id": row["order_id"],
                "parsed_pretty": _pretty(row["parsed_json"]),
                "candidates_pretty": _pretty(row["candidate_ynab_txn_ids"]),
            }
        )

    ambiguous = []
    for row in db.list_orders("ambiguous"):
        candidates = json.loads(row["candidate_ynab_txn_ids"]) if row["candidate_ynab_txn_ids"] else []
        ambiguous.append(
            {
                "order_id": row["order_id"],
                "parsed_pretty": _pretty(row["parsed_json"]),
                "candidates": candidates,
            }
        )

    return templates.TemplateResponse(request, "review.html", {"pending": pending, "ambiguous": ambiguous})


@router.post("/orders/{order_id}/approve")
def approve(order_id: str):
    apply_patch(order_id, allow_reapply=False)
    return RedirectResponse(url="/review", status_code=303)


@router.post("/orders/{order_id}/pick-candidate")
def pick(order_id: str, txn_id: str = Form(...)):
    pick_candidate(order_id, txn_id)
    return RedirectResponse(url="/review", status_code=303)


@router.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return templates.TemplateResponse(
        request, "history.html", {"orders": db.list_orders(), "runs": db.list_runs()}
    )


@router.get("/receipts/{order_id}", response_class=HTMLResponse)
def receipt_detail(request: Request, order_id: str):
    order = db.get_order(order_id)
    if order is None:
        return PlainTextResponse("Order not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "receipt_detail.html",
        {
            "order": order,
            "parsed_pretty": _pretty(order["parsed_json"]),
            "payload_pretty": _pretty(order["ynab_patch_payload"]),
            "apply_log": db.get_apply_log(order_id),
            "allow_reset": settings.allow_reset,
            "allow_reapply": settings.allow_reapply,
        },
    )


@router.get("/receipts/{order_id}/html", response_class=HTMLResponse)
def receipt_html(order_id: str):
    """Serves the raw scraped Amazon page. This is untrusted content rendered
    on the dashboard's own origin, which has unauthenticated state-changing
    POST routes (approve, reapply, create-transaction) — a <script> in the
    saved page could otherwise fire those. `sandbox` makes scripts/forms
    inert while the page still renders normally (docs/IMPROVEMENTS.md item 4)."""
    order = db.get_order(order_id)
    if order is None:
        return PlainTextResponse("Order not found", status_code=404)
    return HTMLResponse(
        Path(order["html_path"]).read_text(),
        headers={"Content-Security-Policy": "sandbox"},
    )


@router.post("/orders/{order_id}/reset")
def reset(order_id: str, target: str = Form(...)):
    reset_order(order_id, target)
    return RedirectResponse(url=f"/receipts/{order_id}", status_code=303)


@router.post("/orders/{order_id}/reapply")
def reapply(order_id: str, confirm: str = Form(...)):
    apply_patch(order_id, allow_reapply=True)
    return RedirectResponse(url=f"/receipts/{order_id}", status_code=303)
