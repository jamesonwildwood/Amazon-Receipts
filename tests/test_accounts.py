import logging

import pytest

from app import accounts as accounts_module
from app.config import settings


@pytest.fixture(autouse=True)
def _clean_amazon_config(monkeypatch):
    """Every test starts from "nothing configured" regardless of what a real
    .env/amazon_accounts.toml on this machine might otherwise contribute --
    none should exist in this worktree, but be explicit rather than rely on
    that."""
    monkeypatch.setattr(settings, "amazon_email", "")
    monkeypatch.setattr(settings, "amazon_password", "")
    monkeypatch.setattr(settings, "amazon_totp_secret", "")
    monkeypatch.setattr(settings, "amazon_accounts_path", "/nonexistent/amazon_accounts.toml")


def _write_toml(tmp_path, content):
    path = tmp_path / "amazon_accounts.toml"
    path.write_text(content)
    return path


def test_load_accounts_returns_empty_when_nothing_configured():
    assert accounts_module.load_accounts() == []


def test_load_accounts_falls_back_to_env_vars_as_default_account(monkeypatch):
    monkeypatch.setattr(settings, "amazon_email", "a@example.com")
    monkeypatch.setattr(settings, "amazon_password", "pw")
    monkeypatch.setattr(settings, "amazon_totp_secret", "SECRET")

    result = accounts_module.load_accounts()

    assert len(result) == 1
    assert result[0].label == "default"
    assert result[0].email == "a@example.com"
    assert result[0].password == "pw"
    assert result[0].totp_secret == "SECRET"
    assert result[0].ynab_account_id is None


def test_load_accounts_env_fallback_requires_both_email_and_password(monkeypatch):
    monkeypatch.setattr(settings, "amazon_email", "a@example.com")
    # password left unset
    assert accounts_module.load_accounts() == []


def test_load_accounts_reads_toml(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "jameson"
email = "jameson@example.com"
password = "pw1"
totp_secret = "SECRET1"

[[accounts]]
label = "spouse"
email = "spouse@example.com"
password = "pw2"
ynab_account_id = "acct-override"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    result = accounts_module.load_accounts()

    assert [a.label for a in result] == ["jameson", "spouse"]
    assert result[0].totp_secret == "SECRET1"
    assert result[1].totp_secret == ""  # optional, defaults to empty
    assert result[0].ynab_account_id is None
    assert result[1].ynab_account_id == "acct-override"


def test_load_accounts_toml_wins_over_env_and_logs_warning(tmp_path, monkeypatch, caplog):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "toml-account"
email = "toml@example.com"
password = "pw"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))
    monkeypatch.setattr(settings, "amazon_email", "env@example.com")
    monkeypatch.setattr(settings, "amazon_password", "pw")

    with caplog.at_level(logging.WARNING):
        result = accounts_module.load_accounts()

    assert [a.label for a in result] == ["toml-account"]
    assert any("wins" in r.message for r in caplog.records)


def test_load_accounts_rejects_duplicate_labels(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "same"
email = "a@example.com"
password = "pw"

[[accounts]]
label = "same"
email = "b@example.com"
password = "pw"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match="duplicate"):
        accounts_module.load_accounts()


def test_load_accounts_rejects_missing_password(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "no-password"
email = "a@example.com"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match="password"):
        accounts_module.load_accounts()


def test_load_accounts_rejects_missing_email(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "no-email"
password = "pw"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match="email"):
        accounts_module.load_accounts()


def test_load_accounts_rejects_missing_label(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
email = "a@example.com"
password = "pw"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match="label"):
        accounts_module.load_accounts()


def test_load_accounts_rejects_unsafe_label(tmp_path, monkeypatch):
    toml_path = _write_toml(
        tmp_path,
        """
[[accounts]]
label = "not safe!"
email = "a@example.com"
password = "pw"
""",
    )
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match="filesystem-safe"):
        accounts_module.load_accounts()


def test_load_accounts_rejects_empty_accounts_list(tmp_path, monkeypatch):
    toml_path = _write_toml(tmp_path, "")
    monkeypatch.setattr(settings, "amazon_accounts_path", str(toml_path))

    with pytest.raises(ValueError, match=r"no \[\[accounts\]\]"):
        accounts_module.load_accounts()


def test_ynab_account_id_for_label_uses_override_else_global(monkeypatch):
    monkeypatch.setattr(settings, "ynab_account_id", "global-acct")
    configured = [
        accounts_module.AmazonAccount(label="a", email="a@x.com", password="pw", ynab_account_id="override-1"),
        accounts_module.AmazonAccount(label="b", email="b@x.com", password="pw"),
    ]

    assert accounts_module.ynab_account_id_for_label("a", configured) == "override-1"
    assert accounts_module.ynab_account_id_for_label("b", configured) == "global-acct"


def test_ynab_account_id_for_label_falls_back_to_global_for_unknown_label(monkeypatch):
    monkeypatch.setattr(settings, "ynab_account_id", "global-acct")
    assert accounts_module.ynab_account_id_for_label("unknown", []) == "global-acct"
