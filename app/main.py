import logging
from contextlib import asynccontextmanager
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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Same two startup steps as before (docs/IMPROVEMENTS.md Extra 2 —
    # @app.on_event("startup") is deprecated in favor of this lifespan
    # context; same order preserved: scheduler first, then the stale-run
    # sweep). Must run single-worker only — multiple uvicorn workers would
    # start duplicate schedulers.
    start_scheduler()

    # A prior process crash (kill -9, OOM, power loss) leaves a 'running' row
    # behind forever — the in-memory run lock resets on restart, but the DB
    # row doesn't. Without this, Run Now stays permanently disabled.
    fixed = mark_stale_runs_as_error(STALE_RUN_AFTER_HOURS)
    if fixed:
        logger.warning("Marked %d stale 'running' pipeline run(s) as error at startup", fixed)

    yield
    # No shutdown behavior needed today -- BackgroundScheduler's worker
    # threads are daemonic and exit with the process.


app = FastAPI(title="Amazon Receipts → YNAB", lifespan=lifespan)
app.add_middleware(RejectCrossOriginWrites)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")), name="static")
app.include_router(dashboard_router)
