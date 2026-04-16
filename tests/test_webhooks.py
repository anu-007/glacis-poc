"""Tests for the POST /api/v1/webhook ingestion endpoint."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

WEBHOOK_URL = "/api/v1/webhook"

SHIPMENT_PAYLOAD = {
    "vendorId": "vendor-42",
    "trackingNumber": "TRK-999",
    "status": "TRANSIT",
    "timestamp": "2024-06-01T12:00:00Z",
    "someExtraField": "ignored",
}

INVOICE_PAYLOAD = {
    "vendorId": "vendor-42",
    "invoiceId": "INV-001",
    "amount": 199.99,
    "currency": "USD",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_webhook_accepted(client):
    with patch("app.routes.webhooks.enqueue_event", return_value="job-abc") as mock_enqueue:
        resp = client.post(WEBHOOK_URL, json=SHIPMENT_PAYLOAD)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["event_id"] is not None
    assert body["job_id"] == "job-abc"
    mock_enqueue.assert_called_once()


def test_webhook_stores_raw_event(client, db_session):
    from app.models import RawEvent

    with patch("app.routes.webhooks.enqueue_event", return_value="job-1"):
        client.post(WEBHOOK_URL, json=INVOICE_PAYLOAD)

    events = db_session.query(RawEvent).all()
    assert len(events) == 1
    assert events[0].payload == INVOICE_PAYLOAD


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_duplicate_webhook_rejected(client):
    with patch("app.routes.webhooks.enqueue_event", return_value="job-dup"):
        resp1 = client.post(WEBHOOK_URL, json=SHIPMENT_PAYLOAD)
        resp2 = client.post(WEBHOOK_URL, json=SHIPMENT_PAYLOAD)

    assert resp1.status_code == 202
    assert resp1.json()["status"] == "accepted"
    assert resp2.status_code == 202
    assert resp2.json()["status"] == "duplicate"


def test_different_payloads_both_accepted(client):
    payload_a = {"vendorId": "v1", "trackingNumber": "TRK-A", "status": "TRANSIT", "timestamp": "2024-01-01T00:00:00Z"}
    payload_b = {"vendorId": "v2", "trackingNumber": "TRK-B", "status": "DELIVERED", "timestamp": "2024-01-02T00:00:00Z"}

    with patch("app.routes.webhooks.enqueue_event", return_value="job-x"):
        resp1 = client.post(WEBHOOK_URL, json=payload_a)
        resp2 = client.post(WEBHOOK_URL, json=payload_b)

    assert resp1.json()["status"] == "accepted"
    assert resp2.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Bad requests
# ---------------------------------------------------------------------------


def test_non_json_body_rejected(client):
    resp = client.post(
        WEBHOOK_URL,
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_json_array_rejected(client):
    resp = client.post(WEBHOOK_URL, json=[1, 2, 3])
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Enqueue failure handling
# ---------------------------------------------------------------------------


def test_enqueue_failure_returns_500(client):
    with patch("app.routes.webhooks.enqueue_event", side_effect=RuntimeError("redis down")):
        resp = client.post(WEBHOOK_URL, json=SHIPMENT_PAYLOAD)
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
