import os
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = ":memory:"

import httpx
import pytest
from fastapi.testclient import TestClient

import relay

SLOT = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
GOOD = {"service": "plumbing", "name": "Test Customer", "email": "test@example.com",
        "phone": "+91 90000 00000", "preferred_time": SLOT}


class External:
    """Fake external service: records calls, answers with the given statuses in turn."""

    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, request):
        self.calls.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status == "down":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(status, json={"ok": status < 400})


@pytest.fixture
def client(monkeypatch):
    relay.db.execute("DELETE FROM requests")
    monkeypatch.setattr(relay, "RETRY_DELAY", 0)
    return TestClient(relay.app)


def use(monkeypatch, external):
    monkeypatch.setattr(relay, "HTTP_TRANSPORT", httpx.MockTransport(external))
    return external


def test_successful_submission(client, monkeypatch):
    ext = use(monkeypatch, External(200))
    r = client.post("/requests", json=GOOD)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "created" and body["reference"].startswith("REQ-")
    assert len(ext.calls) == 1 and ext.calls[0].headers["Idempotency-Key"]
    assert client.get(f"/requests/{body['reference']}").json()["status"] == "created"


def test_incomplete_data_is_rejected_and_never_sent(client, monkeypatch):
    ext = use(monkeypatch, External(200))
    r = client.post("/requests", json={"service": "massage", "name": "T", "preferred_time": "tomorrow 3pm"})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert fields == {"service", "name", "email", "preferred_time"}
    assert ext.calls == []


@pytest.mark.parametrize("time", ["2020-01-01T10:00:00+00:00", "2030-01-01T10:00:00"])
def test_past_or_timezoneless_time_is_rejected(client, monkeypatch, time):
    use(monkeypatch, External(200))
    r = client.post("/requests", json={**GOOD, "preferred_time": time})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "preferred_time"


def test_repeated_submission_creates_one_request(client, monkeypatch):
    ext = use(monkeypatch, External(200))
    first = client.post("/requests", json=GOOD)
    again = client.post("/requests", json={**GOOD, "name": "test customer ", "email": "TEST@example.com"})
    assert first.status_code == 201
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    assert again.json()["reference"] == first.json()["reference"]
    assert len(ext.calls) == 1


def test_explicit_idempotency_key_is_honoured(client, monkeypatch):
    ext = use(monkeypatch, External(200))
    h = {"Idempotency-Key": "abc-123"}
    assert client.post("/requests", json=GOOD, headers=h).status_code == 201
    assert client.post("/requests", json=GOOD, headers=h).json()["status"] == "duplicate"
    assert len(ext.calls) == 1


@pytest.mark.parametrize("failure", [503, "down"])
def test_unavailable_service_retries_then_reports_and_allows_retry(client, monkeypatch, failure):
    ext = use(monkeypatch, External(failure))
    r = client.post("/requests", json=GOOD)
    assert r.status_code == 503 and r.json() == {**r.json(), "status": "unavailable", "retryable": True}
    assert len(ext.calls) == relay.ATTEMPTS
    assert relay.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0  # nothing half-created

    # Service is back: the same request goes through once, not blocked as a duplicate.
    ext = use(monkeypatch, External(200))
    assert client.post("/requests", json=GOOD).status_code == 201
    assert len(ext.calls) == 1


def test_external_rejection_is_not_retried(client, monkeypatch):
    ext = use(monkeypatch, External(400))
    r = client.post("/requests", json=GOOD)
    assert r.status_code == 502 and r.json()["retryable"] is False
    assert len(ext.calls) == 1


def test_recovers_within_retries(client, monkeypatch):
    ext = use(monkeypatch, External(502, 200))
    assert client.post("/requests", json=GOOD).status_code == 201
    assert len(ext.calls) == 2


def test_api_key_is_required_when_set(client, monkeypatch):
    ext = use(monkeypatch, External(200))
    monkeypatch.setattr(relay, "API_KEY", "s3cret")
    assert client.post("/requests", json=GOOD).status_code == 401
    assert client.post("/requests", json=GOOD, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/requests", json=GOOD, headers={"Authorization": "Bearer s3cret"}).status_code == 201
    assert len(ext.calls) == 1
