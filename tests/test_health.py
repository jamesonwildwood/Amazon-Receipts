import pytest

from app import db, health, notify
from app.config import settings


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


class _HTTPErrorWithStatus(Exception):
    """Stand-in for requests.exceptions.HTTPError: health.py only ever reads
    exc.response.status_code, so a minimal fake with that shape is enough and
    keeps this test offline."""

    def __init__(self, status_code):
        super().__init__(f"simulated HTTP {status_code}")
        self.response = type("Resp", (), {"status_code": status_code})()


# --- check_ynab_config ---

def test_check_ynab_config_skipped_when_no_token_configured(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "")
    calls = []
    monkeypatch.setattr(health.ynab_client, "get_accounts", lambda: calls.append(1))

    health.check_ynab_config()

    assert calls == []  # never even called
    assert db.get_config_health("ynab") is None  # not recorded either way


def test_check_ynab_config_healthy_records_ok(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "real-token")
    monkeypatch.setattr(health.ynab_client, "get_accounts", lambda: [{"id": "acct-1"}])
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append(subject))

    health.check_ynab_config()

    row = db.get_config_health("ynab")
    assert row["ok"] == 1
    assert sent == []  # healthy -- quiet


def test_check_ynab_config_401_records_failure_and_notifies(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "revoked-token")

    def _raise():
        raise _HTTPErrorWithStatus(401)

    monkeypatch.setattr(health.ynab_client, "get_accounts", _raise)
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append((subject, body)))

    health.check_ynab_config()

    row = db.get_config_health("ynab")
    assert row["ok"] == 0
    assert "YNAB_PERSONAL_ACCESS_TOKEN" in row["message"]
    assert len(sent) == 1
    assert "YNAB" in sent[0][0]


def test_check_ynab_config_404_names_budget_id(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "token")

    def _raise():
        raise _HTTPErrorWithStatus(404)

    monkeypatch.setattr(health.ynab_client, "get_accounts", _raise)
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append((subject, body)))

    health.check_ynab_config()

    row = db.get_config_health("ynab")
    assert row["ok"] == 0
    assert "YNAB_BUDGET_ID" in row["message"]
    assert len(sent) == 1


def test_check_ynab_config_non_config_error_does_not_flip_banner_or_notify(temp_db, monkeypatch):
    """A 429/500/network blip is not a config problem -- the pipeline's own
    retry/error handling covers that; this check must not falsely accuse a
    setting of being wrong."""
    monkeypatch.setattr(settings, "ynab_personal_access_token", "token")

    def _raise():
        raise _HTTPErrorWithStatus(500)

    monkeypatch.setattr(health.ynab_client, "get_accounts", _raise)
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append(subject))

    health.check_ynab_config()

    assert db.get_config_health("ynab") is None  # never recorded either way
    assert sent == []


def test_check_ynab_config_clears_a_prior_failure_once_healthy_again(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "token")
    db.record_config_health("ynab", False, "previously broken")

    monkeypatch.setattr(health.ynab_client, "get_accounts", lambda: [])

    health.check_ynab_config()

    assert db.get_config_health("ynab")["ok"] == 1


# --- check_amazon_accounts_config ---

def test_check_amazon_accounts_config_healthy_with_nothing_configured(temp_db, monkeypatch):
    # No amazon_accounts.toml, no legacy env vars -- load_accounts() returns
    # [] without raising; that's healthy, not a failure.
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append(subject))

    health.check_amazon_accounts_config()

    assert db.get_config_health("amazon_accounts")["ok"] == 1
    assert sent == []


def test_check_amazon_accounts_config_records_failure_and_notifies(temp_db, monkeypatch):
    def _raise():
        raise ValueError("amazon_accounts.toml: duplicate account label(s): jameson")

    monkeypatch.setattr(health, "load_accounts", _raise)
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda subject, body: sent.append((subject, body)))

    health.check_amazon_accounts_config()

    row = db.get_config_health("amazon_accounts")
    assert row["ok"] == 0
    assert "duplicate account label" in row["message"]
    assert len(sent) == 1


# --- list_failing_config_checks (Home banner) ---

def test_list_failing_config_checks_only_returns_failing_rows(temp_db):
    db.record_config_health("ynab", True, None)
    db.record_config_health("amazon_accounts", False, "broken toml")

    failing = db.list_failing_config_checks()

    assert [r["check_name"] for r in failing] == ["amazon_accounts"]


def test_run_startup_checks_runs_both_checks(temp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(health, "check_ynab_config", lambda: calls.append("ynab"))
    monkeypatch.setattr(health, "check_amazon_accounts_config", lambda: calls.append("amazon_accounts"))

    health.run_startup_checks()

    assert calls == ["ynab", "amazon_accounts"]
