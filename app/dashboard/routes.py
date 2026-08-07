import datetime as dt
import json
import threading
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.config import settings
from app.dashboard import formatting
from app.pipeline import STALE_RUN_AFTER_HOURS, run_pipeline
from app.scheduler import get_next_run_time
from app.ynab.apply import apply_patch, create_transaction, reset_order
from app.ynab.matcher import pick_candidate

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["badge"] = formatting.badge
templates.env.filters["money"] = formatting.format_cents
templates.env.filters["moneymilli"] = formatting.format_milliunits
templates.env.filters["reltime"] = formatting.format_reltime
templates.env.filters["shortid"] = formatting.truncate_id


def _pretty(json_text: str | None) -> str:
    if not json_text:
        return "—"
    try:
        return json.dumps(json.loads(json_text), indent=2)
    except (TypeError, ValueError):
        return json_text


def _redirect_with_flash(url: str, reason: str | None, status_code: int = 303) -> RedirectResponse:
    message, tone = formatting.apply_reason_message(reason)
    qs = urlencode({"flash": message, "tone": tone})
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{sep}{qs}", status_code=status_code)


def _flash_context(request: Request) -> dict:
    flash = request.query_params.get("flash")
    tone = request.query_params.get("tone", "neutral")
    return {"flash": flash, "flash_tone": tone}


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


def _receipt_items(parsed_json: str | None) -> list[dict]:
    """Flattens parsed_json into a plain list of {title, price, category} for
    a readable items table, instead of a raw JSON dump."""
    if not parsed_json:
        return []
    try:
        data = json.loads(parsed_json)
    except (TypeError, ValueError):
        return []
    return data.get("items", [])


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    last_run = db.get_last_run()
    counts = db.count_by_match_status()
    parse_error_count = len(db.list_parse_error_orders())

    needs_attention = list(db.list_orders_by_statuses(("pending_review", "ambiguous", "error"))) + list(
        db.list_parse_error_orders()
    )
    needs_attention.sort(key=lambda o: o["created_at"], reverse=True)

    with db.connect() as conn:
        last_scrape_at = conn.execute("SELECT MAX(scraped_at) as m FROM amazon_orders").fetchone()["m"]
        last_ynab_success_at = conn.execute(
            "SELECT MAX(applied_at) as m FROM ynab_apply_log WHERE success = 1"
        ).fetchone()["m"]

    dev_flags_on = [
        name
        for name, on in [
            ("ALLOW_RESET", settings.allow_reset),
            ("ALLOW_REAPPLY", settings.allow_reapply),
            ("YNAB_ALLOW_CREATE_WITHOUT_MATCH", settings.ynab_allow_create_without_match),
        ]
        if on
    ]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **_flash_context(request),
            "last_run": last_run,
            "run_in_progress": _run_in_progress(last_run),
            "next_run_time": get_next_run_time(),
            "counts": counts,
            "parse_error_count": parse_error_count,
            "needs_attention": needs_attention,
            "reapplied_count": db.count_reapplied(),
            "last_scrape_at": last_scrape_at,
            "last_ynab_success_at": last_ynab_success_at,
            "recent_runs": db.list_runs(limit=7),
            "dev_flags_on": dev_flags_on,
            "settings": settings,
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
        payload = json.loads(row["ynab_patch_payload"]) if row["ynab_patch_payload"] else {}
        candidates = json.loads(row["candidate_ynab_txn_ids"]) if row["candidate_ynab_txn_ids"] else []
        pending.append(
            {
                "order_id": row["order_id"],
                "order_date": row["order_date"],
                "line_items": _receipt_items(row["parsed_json"]),
                "grand_total_cents": row["grand_total_cents"],
                "payload": payload,
                "candidate": candidates[0] if candidates else None,
            }
        )

    ambiguous = []
    for row in db.list_orders("ambiguous"):
        candidates = json.loads(row["candidate_ynab_txn_ids"]) if row["candidate_ynab_txn_ids"] else []
        ambiguous.append(
            {
                "order_id": row["order_id"],
                "order_date": row["order_date"],
                "line_items": _receipt_items(row["parsed_json"]),
                "grand_total_cents": row["grand_total_cents"],
                "candidates": candidates,
            }
        )

    return templates.TemplateResponse(
        request, "review.html", {**_flash_context(request), "pending": pending, "ambiguous": ambiguous}
    )


@router.post("/orders/{order_id}/approve")
def approve(order_id: str):
    result = apply_patch(order_id, allow_reapply=False)
    return _redirect_with_flash("/review", result.reason)


@router.post("/orders/{order_id}/reject")
def reject(order_id: str):
    ok = db.mark_rejected(order_id)
    return _redirect_with_flash("/review", "rejected" if ok else "reject_failed")


@router.post("/orders/{order_id}/pick-candidate")
def pick(order_id: str, txn_id: str = Form(...)):
    pick_candidate(order_id, txn_id)
    return RedirectResponse(url="/review", status_code=303)


@router.post("/orders/{order_id}/create-transaction")
def create_transaction_route(order_id: str, confirm: str = Form(...)):
    result = create_transaction(order_id)
    return _redirect_with_flash(f"/receipts/{order_id}", result.reason)


@router.get("/history", response_class=HTMLResponse)
def history(request: Request):
    status_filter = request.query_params.get("status")
    orders = db.list_orders(status_filter) if status_filter else db.list_orders()
    all_counts = db.count_by_match_status()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            **_flash_context(request),
            "orders": orders,
            "runs": db.list_runs(),
            "status_filter": status_filter,
            "all_counts": all_counts,
            "total_count": sum(all_counts.values()),
        },
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
            **_flash_context(request),
            "order": order,
            "items": _receipt_items(order["parsed_json"]),
            "payload_pretty": _pretty(order["ynab_patch_payload"]),
            "apply_log": db.get_apply_log(order_id),
            "allow_reset": settings.allow_reset,
            "allow_reapply": settings.allow_reapply,
            "allow_create_without_match": settings.ynab_allow_create_without_match,
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
    ok = reset_order(order_id, target)
    return _redirect_with_flash(f"/receipts/{order_id}", "reset_applied" if ok else "reset_disabled")


@router.post("/orders/{order_id}/reapply")
def reapply(order_id: str, confirm: str = Form(...)):
    result = apply_patch(order_id, allow_reapply=True)
    return _redirect_with_flash(f"/receipts/{order_id}", result.reason)


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request):
    lines = int(request.query_params.get("lines", 200))
    log_path = Path(settings.database_path).resolve().parent / "logs" / "app.log"
    if log_path.exists():
        content = log_path.read_text().splitlines()[-lines:]
    else:
        content = ["(no log file yet — nothing has run since this app started)"]
    return templates.TemplateResponse(
        request, "logs.html", {**_flash_context(request), "lines": lines, "log_lines": content}
    )
