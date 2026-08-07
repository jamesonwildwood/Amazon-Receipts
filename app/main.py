from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.dashboard.routes import router as dashboard_router
from app.dashboard.security import RejectCrossOriginWrites
from app.db import init_db, mark_stale_runs_as_error
from app.logging_setup import configure_logging
from app.pipeline import STALE_RUN_AFTER_HOURS
from app.scheduler import start_scheduler

configure_logging()
init_db()

app = FastAPI(title="Amazon Receipts → YNAB")
app.add_middleware(RejectCrossOriginWrites)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")), name="static")
app.include_router(dashboard_router)


@app.on_event("startup")
def _start_scheduler() -> None:
    start_scheduler()


@app.on_event("startup")
def _clear_stale_runs() -> None:
    # A prior process crash (kill -9, OOM, power loss) leaves a 'running' row
    # behind forever — the in-memory run lock resets on restart, but the DB
    # row doesn't. Without this, Run Now stays permanently disabled.
    fixed = mark_stale_runs_as_error(STALE_RUN_AFTER_HOURS)
    if fixed:
        import logging

        logging.getLogger(__name__).warning("Marked %d stale 'running' pipeline run(s) as error at startup", fixed)
