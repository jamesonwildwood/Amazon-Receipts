from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.dashboard.routes import router as dashboard_router
from app.db import init_db
from app.logging_setup import configure_logging

configure_logging()
init_db()

app = FastAPI(title="Amazon Receipts → YNAB")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")), name="static")
app.include_router(dashboard_router)
