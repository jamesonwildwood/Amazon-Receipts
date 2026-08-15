import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import health
from app.config import settings
from app.pipeline import run_pipeline

logger = logging.getLogger(__name__)

JOB_ID = "amazon_receipts_pipeline"
_scheduler: Optional[BackgroundScheduler] = None


def _parse_cron(expr: str) -> CronTrigger:
    minute, hour, day, month, day_of_week = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


def start_scheduler() -> BackgroundScheduler:
    """Runs run_pipeline() on Selenium's own blocking thread (BackgroundScheduler,
    not asyncio) so it never stalls the dashboard's event loop. max_instances=1 is
    a second, scheduler-level guard against overlapping runs, on top of the DB-level
    atomic claim in app/ynab/apply.py — belt and suspenders, not a replacement."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    # Once at scheduler startup, not just at the start of every pipeline run
    # -- catches a revoked YNAB token or broken amazon_accounts.toml the
    # moment the app comes up, not just the next time a run happens to fire
    # (docs/IMPROVEMENTS.md 5.5). Never allowed to block startup itself.
    try:
        health.run_startup_checks()
    except Exception:
        logger.exception("Config sanity checks failed unexpectedly at scheduler startup")

    scheduler = BackgroundScheduler()
    trigger = _parse_cron(settings.pipeline_schedule_cron)
    scheduler.add_job(run_pipeline, trigger, id=JOB_ID, max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started with cron '%s'", settings.pipeline_schedule_cron)
    _scheduler = scheduler
    return scheduler


def get_next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None
