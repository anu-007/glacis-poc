"""Tests for the background worker: process_event, DLQ routing, schema validation."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app import repository
from app.models import DLQEvent, NormalizedEvent, RawEvent
from app.worker import process_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(db_session, payload: dict) -> RawEvent:
    import hashlib, json
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    raw = RawEvent(payload=payload, payload_hash=h)
    db_session.add(raw)
    db_session.commit()
    return raw


# ---------------------------------------------------------------------------
# Shipment happy path
# ---------------------------------------------------------------------------


def test_process_shipment_event(db_session, db_session_factory):
    payload = {
        "vendorId": "v1",
        "trackingNumber": "TRK-100",
        "status": "TRANSIT",
        "timestamp": "2024-06-01T12:00:00Z",
    }
    raw = _make_raw(db_session, payload)
    raw_id = raw.id  # capture before potential expiry

    with patch("app.worker.SessionLocal", db_session_factory):
        process_event(str(raw_id))

    normed = db_session.query(NormalizedEvent).first()
    assert normed is not None
    assert normed.type == "SHIPMENT"
    assert normed.raw_event_id == raw_id
    assert db_session.query(DLQEvent).count() == 0


# ---------------------------------------------------------------------------
# Invoice happy path
# ---------------------------------------------------------------------------


def test_process_invoice_event(db_session, db_session_factory):
    payload = {
        "vendorId": "v2",
        "invoiceId": "INV-555",
        "amount": 999.99,
        "currency": "EUR",
    }
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    with patch("app.worker.SessionLocal", db_session_factory):
        process_event(str(raw_id))

    normed = db_session.query(NormalizedEvent).first()
    assert normed is not None
    assert normed.type == "INVOICE"
    assert normed.data["amount"] == 999.99


# ---------------------------------------------------------------------------
# Unclassified event
# ---------------------------------------------------------------------------


def test_process_unclassified_event(db_session, db_session_factory):
    payload = {"foo": "bar", "baz": 42}
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    with patch("app.worker.SessionLocal", db_session_factory):
        process_event(str(raw_id))

    normed = db_session.query(NormalizedEvent).first()
    assert normed is not None
    assert normed.type == "UNCLASSIFIED"


# ---------------------------------------------------------------------------
# DLQ: LLM failure
# ---------------------------------------------------------------------------


def test_llm_failure_routes_to_dlq(db_session, db_session_factory):
    from app.llm import RetryableError

    payload = {"vendorId": "v3", "trackingNumber": "TRK-ERR", "status": "TRANSIT", "timestamp": "2024-01-01T00:00:00Z"}
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    with patch("app.worker.SessionLocal", db_session_factory), \
         patch("app.worker.process_with_llm", side_effect=RetryableError("LLM timeout")):
        process_event(str(raw_id))

    assert db_session.query(NormalizedEvent).count() == 0
    dlq = db_session.query(DLQEvent).first()
    assert dlq is not None
    assert "LLM" in dlq.error


# ---------------------------------------------------------------------------
# DLQ: schema validation failure
# ---------------------------------------------------------------------------


def test_invalid_schema_routes_to_dlq(db_session, db_session_factory):
    payload = {"vendorId": "v4", "trackingNumber": "TRK-BAD"}
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    # LLM returns a SHIPMENT but with missing required fields
    bad_llm_output = {"type": "SHIPMENT", "vendorId": "v4"}  # missing trackingNumber, status, timestamp

    with patch("app.worker.SessionLocal", db_session_factory), \
         patch("app.worker.process_with_llm", return_value=bad_llm_output):
        process_event(str(raw_id))

    assert db_session.query(NormalizedEvent).count() == 0
    dlq = db_session.query(DLQEvent).first()
    assert dlq is not None
    assert "validation" in dlq.error.lower()


# ---------------------------------------------------------------------------
# Missing event ID
# ---------------------------------------------------------------------------


def test_missing_event_id_is_skipped(db_session, db_session_factory):
    missing_id = str(uuid.uuid4())
    with patch("app.worker.SessionLocal", db_session_factory):
        process_event(missing_id)  # should not raise

    assert db_session.query(NormalizedEvent).count() == 0
    assert db_session.query(DLQEvent).count() == 0


# ---------------------------------------------------------------------------
# Idempotency: processing the same event twice is safe
# ---------------------------------------------------------------------------


def test_idempotent_processing(db_session, db_session_factory):
    """A second call for the same event must be a silent no-op."""
    payload = {
        "vendorId": "v5",
        "trackingNumber": "TRK-IDEM",
        "status": "DELIVERED",
        "timestamp": "2024-01-01T00:00:00Z",
    }
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    with patch("app.worker.SessionLocal", db_session_factory):
        process_event(str(raw_id))
        process_event(str(raw_id))  # second call — must be a no-op

    assert db_session.query(NormalizedEvent).count() == 1
    assert db_session.query(DLQEvent).count() == 0


# ---------------------------------------------------------------------------
# DB retry: transient OperationalError triggers a retry and then succeeds
# ---------------------------------------------------------------------------


def test_db_retry_on_transient_error(db_session, db_session_factory):
    """
    If the first DB call raises OperationalError the worker retries with a
    fresh session and eventually succeeds.
    """
    payload = {
        "vendorId": "v6",
        "trackingNumber": "TRK-RETRY",
        "status": "TRANSIT",
        "timestamp": "2024-01-01T00:00:00Z",
    }
    raw = _make_raw(db_session, payload)
    raw_id = raw.id

    original_fetch = repository.fetch_raw_event
    call_count = {"n": 0}

    def flaky_fetch(db, event_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError("connection lost", {}, Exception())
        return original_fetch(db, event_id)

    with (
        patch("app.worker.SessionLocal", db_session_factory),
        patch("app.repository.fetch_raw_event", flaky_fetch),
        patch("app.worker.time.sleep"),  # skip real sleep in tests
    ):
        process_event(str(raw_id))

    assert db_session.query(NormalizedEvent).count() == 1
    assert db_session.query(DLQEvent).count() == 0
    assert call_count["n"] == 2  # failed once, succeeded once
