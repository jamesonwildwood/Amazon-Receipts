import pytest

from app import __main__ as cli
from app import db
from app.config import settings


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    # configure_logging() writes to a hardcoded ./data/logs relative to cwd --
    # stub it so a CLI test never touches the real repo's data/ directory.
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    db.init_db()
    yield


def _seed_run(status, orders_found=1, orders_parsed=1, orders_matched=1, error_message=None):
    run_id = db.start_run()
    db.finish_run(run_id, status, orders_found, orders_parsed, orders_matched, error_message)
    return run_id


def test_run_command_returns_0_on_success(temp_db, monkeypatch):
    run_id = _seed_run("success")
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: run_id)

    assert cli.main(["run"]) == cli.EXIT_SUCCESS


def test_run_command_returns_1_on_partial(temp_db, monkeypatch):
    run_id = _seed_run("partial", error_message="scrape failed for account(s): spouse")
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: run_id)

    assert cli.main(["run"]) == cli.EXIT_PARTIAL


def test_run_command_returns_2_on_error(temp_db, monkeypatch):
    run_id = _seed_run("error", error_message="boom")
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: run_id)

    assert cli.main(["run"]) == cli.EXIT_ERROR


def test_run_command_returns_2_when_lock_contended(temp_db, monkeypatch):
    """run_pipeline() returning None means another run already holds
    data/pipeline.lock -- the CLI must not treat that as success."""
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: None)

    assert cli.main(["run"]) == cli.EXIT_ERROR


def test_run_command_passes_account_and_headful_through(temp_db, monkeypatch):
    run_id = _seed_run("success")
    captured = {}
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: captured.update(kwargs) or run_id)

    cli.main(["run", "--account", "jameson", "--headful"])

    assert captured == {"account_label": "jameson", "headless": False}


def test_run_command_defaults_pass_none_through(temp_db, monkeypatch):
    """No --account/--headful -> run_pipeline() gets None for both, meaning
    "every configured account" and "use the configured SCRAPE_HEADLESS"."""
    run_id = _seed_run("success")
    captured = {}
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: captured.update(kwargs) or run_id)

    cli.main(["run"])

    assert captured == {"account_label": None, "headless": None}


def test_run_command_reports_missing_run_record_as_error(temp_db, monkeypatch):
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: 999999)  # no such run row

    assert cli.main(["run"]) == cli.EXIT_ERROR


def test_serve_command_invokes_uvicorn_with_configured_host_and_port(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_port", 9999)
    calls = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))

    assert cli.main(["serve"]) == cli.EXIT_SUCCESS
    assert calls[0][0] == ("app.main:app",)
    assert calls[0][1] == {"host": "0.0.0.0", "port": 9999}


def test_serve_command_respects_host_flag(monkeypatch):
    calls = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))

    cli.main(["serve", "--host", "127.0.0.1"])

    assert calls[0][1]["host"] == "127.0.0.1"


def test_missing_command_is_a_usage_error():
    with pytest.raises(SystemExit):
        cli.main([])


def test_unknown_flag_is_a_usage_error():
    with pytest.raises(SystemExit):
        cli.main(["run", "--nope"])
