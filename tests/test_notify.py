from app import notify
from app.config import settings


class _FakeSMTP:
    """Records calls instead of touching a real SMTP server. Supports the
    `with smtplib.SMTP(...) as smtp:` usage in app/notify.py."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_calls = []
        self.sent_messages = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.login_calls.append((user, password))

    def send_message(self, message):
        self.sent_messages.append(message)


def _configure_smtp(monkeypatch, **overrides):
    defaults = dict(
        notify_smtp_host="smtp.gmail.com",
        notify_smtp_port=587,
        notify_smtp_user="me@gmail.com",
        notify_smtp_password="app-password",
        notify_email_from="",
        notify_email_to="me@gmail.com",
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        monkeypatch.setattr(settings, key, value)


def test_send_email_disabled_when_smtp_host_unset(monkeypatch):
    _configure_smtp(monkeypatch, notify_smtp_host="")
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    notify.send_email("subject", "body")

    assert _FakeSMTP.instances == []  # never even tried to connect


def test_send_email_skipped_when_recipient_unset(monkeypatch):
    _configure_smtp(monkeypatch, notify_email_to="")
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    notify.send_email("subject", "body")

    assert _FakeSMTP.instances == []


def test_send_email_sends_via_starttls_with_login(monkeypatch):
    _configure_smtp(monkeypatch)
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    notify.send_email("Test subject", "Test body")

    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.gmail.com"
    assert smtp.port == 587
    assert smtp.starttls_called
    assert smtp.login_calls == [("me@gmail.com", "app-password")]
    assert len(smtp.sent_messages) == 1
    sent = smtp.sent_messages[0]
    assert sent["Subject"] == "Test subject"
    assert sent["To"] == "me@gmail.com"
    assert sent["From"] == "me@gmail.com"


def test_send_email_from_defaults_to_smtp_user_when_from_unset(monkeypatch):
    _configure_smtp(monkeypatch, notify_email_from="")
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    notify.send_email("subject", "body")

    assert _FakeSMTP.instances[0].sent_messages[0]["From"] == "me@gmail.com"


def test_send_email_uses_explicit_from_when_set(monkeypatch):
    _configure_smtp(monkeypatch, notify_email_from="receipts@example.com")
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    notify.send_email("subject", "body")

    assert _FakeSMTP.instances[0].sent_messages[0]["From"] == "receipts@example.com"


def test_send_email_never_raises_on_smtp_failure(monkeypatch):
    """The hard requirement: a notifier failure must never fail or delay the
    pipeline. Any SMTP-layer exception is caught and logged, not re-raised."""
    _configure_smtp(monkeypatch)

    class _BoomSMTP:
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("simulated SMTP connection failure")

    monkeypatch.setattr(notify.smtplib, "SMTP", _BoomSMTP)

    notify.send_email("subject", "body")  # must not raise


def test_send_email_never_raises_when_login_fails(monkeypatch):
    _configure_smtp(monkeypatch)

    class _AuthFailSMTP(_FakeSMTP):
        def login(self, user, password):
            raise Exception("simulated auth failure")

    monkeypatch.setattr(notify.smtplib, "SMTP", _AuthFailSMTP)

    notify.send_email("subject", "body")  # must not raise
