"""
Pydantic schemas for LLM-extracted and normalized webhook events.

The LLM is expected to return a JSON object that matches one of:
  - ShipmentEvent
  - InvoiceEvent
  - UnclassifiedEvent

`parse_llm_output` picks the right schema based on the `type` field.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    SHIPMENT = "SHIPMENT"
    INVOICE = "INVOICE"
    UNCLASSIFIED = "UNCLASSIFIED"


class ShipmentStatus(str, Enum):
    TRANSIT = "TRANSIT"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"


# ---------------------------------------------------------------------------
# Normalized schemas (output of LLM + validation)
# ---------------------------------------------------------------------------


class ShipmentEvent(BaseModel):
    type: Literal[EventType.SHIPMENT] = EventType.SHIPMENT
    vendor_id: str = Field(..., alias="vendorId", min_length=1)
    tracking_number: str = Field(..., alias="trackingNumber", min_length=1)
    status: ShipmentStatus
    timestamp: datetime

    model_config = {"populate_by_name": True}

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v: Any) -> Any:
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class InvoiceEvent(BaseModel):
    type: Literal[EventType.INVOICE] = EventType.INVOICE
    vendor_id: str = Field(..., alias="vendorId", min_length=1)
    invoice_id: str = Field(..., alias="invoiceId", min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)

    model_config = {"populate_by_name": True}

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()


class UnclassifiedEvent(BaseModel):
    type: Literal[EventType.UNCLASSIFIED] = EventType.UNCLASSIFIED
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Union type for validated events
# ---------------------------------------------------------------------------

NormalizedEvent = Union[ShipmentEvent, InvoiceEvent, UnclassifiedEvent]


# ---------------------------------------------------------------------------
# API response schemas
# ---------------------------------------------------------------------------


class WebhookAcceptedResponse(BaseModel):
    status: str
    event_id: str | None = None
    job_id: str | None = None


# ---------------------------------------------------------------------------
# LLM output parser
# ---------------------------------------------------------------------------


def parse_llm_output(data: dict[str, Any]) -> NormalizedEvent:
    """
    Validate LLM-produced dict against the appropriate Pydantic schema.

    Raises ValidationError if the schema does not match.
    Falls back to UnclassifiedEvent if type is missing or unrecognised.
    """
    event_type = str(data.get("type", "UNCLASSIFIED")).upper()

    if event_type == EventType.SHIPMENT:
        return ShipmentEvent.model_validate(data)
    elif event_type == EventType.INVOICE:
        return InvoiceEvent.model_validate(data)
    else:
        return UnclassifiedEvent(type=EventType.UNCLASSIFIED, raw=data)
