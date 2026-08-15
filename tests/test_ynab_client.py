import pytest
import requests

from app.config import settings
from app.ynab import client as ynab_client


class _FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every retry test in this file exercises the sleep-and-retry path --
    never actually sleep in the test suite."""
    monkeypatch.setattr(ynab_client.time, "sleep", lambda seconds: None)


@pytest.fixture
def _token(monkeypatch):
    monkeypatch.setattr(settings, "ynab_personal_access_token", "test-token")
    monkeypatch.setattr(settings, "ynab_budget_id", "budget-1")


def test_get_transactions_retries_on_429_then_succeeds(monkeypatch, _token):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "2"}),
        _FakeResponse(200, json_data={"data": {"transactions": [{"id": "t-1"}]}}),
    ]
    calls = []

    def _fake_request(method, url, timeout=None, **kwargs):
        calls.append(method)
        return responses.pop(0)

    monkeypatch.setattr(requests, "request", _fake_request)

    result = ynab_client.get_transactions_since("acct-1", "2026-01-01")

    assert result == [{"id": "t-1"}]
    assert len(calls) == 2  # one 429, one success


def test_get_gives_up_after_max_attempts_of_429(monkeypatch, _token):
    call_count = {"n": 0}

    def _fake_request(method, url, timeout=None, **kwargs):
        call_count["n"] += 1
        return _FakeResponse(429, headers={"Retry-After": "1"})

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(requests.exceptions.HTTPError):
        ynab_client.get_accounts()

    assert call_count["n"] == 3  # _MAX_ATTEMPTS, not unbounded


def test_retry_sleep_honors_retry_after_capped_at_30s(monkeypatch, _token):
    sleeps = []
    monkeypatch.setattr(ynab_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    responses = [
        _FakeResponse(429, headers={"Retry-After": "5"}),
        _FakeResponse(200, json_data={"data": {"accounts": []}}),
    ]
    monkeypatch.setattr(requests, "request", lambda method, url, timeout=None, **kwargs: responses.pop(0))

    ynab_client.get_accounts()

    assert sleeps == [5.0]


def test_retry_sleep_caps_a_huge_retry_after_at_30_seconds(monkeypatch, _token):
    sleeps = []
    monkeypatch.setattr(ynab_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    responses = [
        _FakeResponse(429, headers={"Retry-After": "300"}),
        _FakeResponse(200, json_data={"data": {"accounts": []}}),
    ]
    monkeypatch.setattr(requests, "request", lambda method, url, timeout=None, **kwargs: responses.pop(0))

    ynab_client.get_accounts()

    assert sleeps == [30.0]


def test_retry_sleep_falls_back_to_cap_when_retry_after_missing(monkeypatch, _token):
    sleeps = []
    monkeypatch.setattr(ynab_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    responses = [
        _FakeResponse(429, headers={}),
        _FakeResponse(200, json_data={"data": {"accounts": []}}),
    ]
    monkeypatch.setattr(requests, "request", lambda method, url, timeout=None, **kwargs: responses.pop(0))

    ynab_client.get_accounts()

    assert sleeps == [30.0]


def test_non_429_error_is_not_retried(monkeypatch, _token):
    call_count = {"n": 0}

    def _fake_request(method, url, timeout=None, **kwargs):
        call_count["n"] += 1
        return _FakeResponse(500)

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(requests.exceptions.HTTPError):
        ynab_client.get_accounts()

    assert call_count["n"] == 1  # no retry at all for a non-429 error


def test_patch_transaction_retries_only_on_429(monkeypatch, _token):
    """The plan explicitly calls out PATCH/POST as safe to retry on 429 (a
    pre-processing rejection, YNAB never touched the write) -- verify the
    same helper covers writes, not just GETs."""
    responses = [
        _FakeResponse(429, headers={"Retry-After": "1"}),
        _FakeResponse(200, json_data={"data": {"transaction": {"id": "txn-1"}}}),
    ]
    calls = []

    def _fake_request(method, url, timeout=None, **kwargs):
        calls.append(method)
        return responses.pop(0)

    monkeypatch.setattr(requests, "request", _fake_request)

    result = ynab_client.patch_transaction("txn-1", {"memo": "hi"})

    assert result == {"id": "txn-1"}
    assert calls == ["PATCH", "PATCH"]


def test_successful_first_call_never_sleeps(monkeypatch, _token):
    sleeps = []
    monkeypatch.setattr(ynab_client.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        requests, "request", lambda method, url, timeout=None, **kwargs: _FakeResponse(200, json_data={"data": {"accounts": []}})
    )

    ynab_client.get_accounts()

    assert sleeps == []
