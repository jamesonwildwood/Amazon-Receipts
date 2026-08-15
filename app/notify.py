"""Email notifications (docs/IMPROVEMENTS.md 5.1) — stdlib smtplib only, no
new dependency, works with a Gmail app password (or any SMTP+STARTTLS
provider). Disabled by default: an empty NOTIFY_SMTP_HOST means "don't send"
rather than "misconfigured", so a fresh install stays quiet until someone
opts in.

A notifier failure must NEVER fail or delay the pipeline — every exception
here is caught and logged, never re-raised. Composition of *what* to send and
*when* (pipeline run outcomes, config sanity failures) lives with the callers
(app/pipeline.py, app/health.py) that hold that context; this module is only
the SMTP mechanism, same separation as app/dashboard/formatting.py keeping
presentation out of routes.py.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT_SECONDS = 15


def send_email(subject: str, body: str) -> None:
    """Best-effort send — logs and returns on any failure (unconfigured,
    unreachable SMTP host, auth failure, etc.) rather than raising, per the
    hard requirement that a notifier bug must never look like a pipeline
    failure."""
    if not settings.notify_smtp_host:
        logger.debug("Email notifications disabled (NOTIFY_SMTP_HOST unset) — skipping %r", subject)
        return
    if not settings.notify_email_to:
        logger.warning("NOTIFY_SMTP_HOST is set but NOTIFY_EMAIL_TO is empty — skipping %r", subject)
        return

    sender = settings.notify_email_from or settings.notify_smtp_user
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = settings.notify_email_to
    message.set_content(body)

    try:
        with smtplib.SMTP(settings.notify_smtp_host, settings.notify_smtp_port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            if settings.notify_smtp_user:
                smtp.login(settings.notify_smtp_user, settings.notify_smtp_password)
            smtp.send_message(message)
    except Exception:
        logger.exception("Failed to send notification email (subject=%r) — continuing regardless", subject)
