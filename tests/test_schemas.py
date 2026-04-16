"""Tests for Pydantic schema validation and parse_llm_output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    EventType,
    InvoiceEvent,
    ShipmentEvent,
    ShipmentStatus,
    UnclassifiedEvent,
    parse_llm_output,
)


# ---------------------------------------------------------------------------
# ShipmentEvent
# ---------------------------------------------------------------------------


def test_shipment_valid():
    data = {
        "type": "SHIPMENT",
        "vendorId": "v1",
        "trackingNumber": "TRK-001",
        "status": "TRANSIT",
        "timestamp": "2024-06-01T12:00:00Z",
    }
    event = ShipmentEvent.model_validate(data)
    assert event.type == EventType.SHIPMENT
    assert event.status == ShipmentStatus.TRANSIT


def test_shipment_invalid_status():
    data = {
        "type": "SHIPMENT",
        "vendorId": "v1",
        "trackingNumber": "TRK-001",
        "status": "FLYING",  # invalid
        "timestamp": "2024-06-01T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        ShipmentEvent.model_validate(data)


def test_shipment_missing_tracking_number():
    data = {
        "type": "SHIPMENT",
        "vendorId": "v1",
        "status": "TRANSIT",
        "timestamp": "2024-06-01T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        ShipmentEvent.model_validate(data)


# ---------------------------------------------------------------------------
# InvoiceEvent
# ---------------------------------------------------------------------------


def test_invoice_valid():
    data = {
        "type": "INVOICE",
        "vendorId": "v2",
        "invoiceId": "INV-099",
        "amount": 250.00,
        "currency": "eur",
    }
    event = InvoiceEvent.model_validate(data)
    assert event.currency == "EUR"
    assert event.amount == 250.00


def test_invoice_negative_amount():
    data = {
        "type": "INVOICE",
        "vendorId": "v2",
        "invoiceId": "INV-099",
        "amount": -10.0,
        "currency": "USD",
    }
    with pytest.raises(ValidationError):
        InvoiceEvent.model_validate(data)


def test_invoice_zero_amount():
    data = {
        "type": "INVOICE",
        "vendorId": "v2",
        "invoiceId": "INV-099",
        "amount": 0.0,
        "currency": "USD",
    }
    with pytest.raises(ValidationError):
        InvoiceEvent.model_validate(data)


# ---------------------------------------------------------------------------
# parse_llm_output
# ---------------------------------------------------------------------------


def test_parse_llm_shipment():
    raw = {
        "type": "SHIPMENT",
        "vendorId": "v1",
        "trackingNumber": "TRK-X",
        "status": "DELIVERED",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    event = parse_llm_output(raw)
    assert isinstance(event, ShipmentEvent)


def test_parse_llm_invoice():
    raw = {
        "type": "INVOICE",
        "vendorId": "v1",
        "invoiceId": "INV-1",
        "amount": 100.0,
        "currency": "GBP",
    }
    event = parse_llm_output(raw)
    assert isinstance(event, InvoiceEvent)


def test_parse_llm_unclassified():
    raw = {"type": "UNCLASSIFIED", "raw": {"foo": "bar"}}
    event = parse_llm_output(raw)
    assert isinstance(event, UnclassifiedEvent)


def test_parse_llm_unknown_type_becomes_unclassified():
    raw = {"type": "MYSTERY", "data": "whatever"}
    event = parse_llm_output(raw)
    assert isinstance(event, UnclassifiedEvent)


def test_parse_llm_missing_type_becomes_unclassified():
    raw = {"someKey": "someValue"}
    event = parse_llm_output(raw)
    assert isinstance(event, UnclassifiedEvent)


def test_parse_llm_invalid_shipment_raises():
    # type says SHIPMENT but fields are wrong → ValidationError
    raw = {"type": "SHIPMENT", "vendorId": "v1"}
    with pytest.raises(Exception):
        parse_llm_output(raw)
