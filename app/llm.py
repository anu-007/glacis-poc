"""
LLM integration for event classification and data extraction.

Supports two modes controlled by settings.llm_mode:
  - "openai"  : calls the OpenAI Chat Completions API
  - "mock"    : returns deterministic fake responses (for dev / tests)
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetryableError(Exception):
    """Raised when the LLM call should be retried."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a webhook event classifier for a supply chain platform.

Given an arbitrary JSON payload, your job is to:
1. Classify the event as one of: SHIPMENT, INVOICE, or UNCLASSIFIED.
2. Extract and return the relevant fields in a strict JSON format.

For SHIPMENT events return:
{
  "type": "SHIPMENT",
  "vendorId": "<string>",
  "trackingNumber": "<string>",
  "status": "TRANSIT | DELIVERED | EXCEPTION",
  "timestamp": "<ISO8601>"
}

For INVOICE events return:
{
  "type": "INVOICE",
  "vendorId": "<string>",
  "invoiceId": "<string>",
  "amount": <float>,
  "currency": "<3-letter ISO code>"
}

For anything else return:
{
  "type": "UNCLASSIFIED",
  "raw": <original payload>
}

Respond ONLY with valid JSON. Do not include any explanation or markdown.
"""


# ---------------------------------------------------------------------------
# OpenAI call (real)
# ---------------------------------------------------------------------------


def _call_openai(payload: dict[str, Any]) -> dict[str, Any]:
    """Call OpenAI API with timeout and basic retry logic."""
    try:
        from openai import OpenAI, APITimeoutError, APIError
    except ImportError as e:
        raise RetryableError("openai package not installed") from e

    client = OpenAI(api_key=settings.openai_api_key, timeout=5.0)

    last_exc: Exception | None = None
    for attempt in range(settings.max_retries):
        try:
            response = client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except (APITimeoutError, json.JSONDecodeError) as exc:
            logger.warning("LLM attempt %d failed: %s", attempt + 1, exc)
            last_exc = exc
        except APIError as exc:
            logger.error("Unrecoverable LLM API error: %s", exc)
            raise RetryableError(str(exc)) from exc

    raise RetryableError(f"LLM failed after {settings.max_retries} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Mock LLM (for dev / tests)
# ---------------------------------------------------------------------------

_MOCK_SHIPMENT_KEYS = {"trackingNumber", "tracking_number", "tracking"}
_MOCK_INVOICE_KEYS = {"invoiceId", "invoice_id", "invoice_number"}


def _call_mock(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Mock LLM that infers event type from payload keys.

    Simulates real LLM behaviour when configured:
      - MOCK_DELAY_MIN / MOCK_DELAY_MAX  → random latency (seconds)
      - MOCK_ERROR_RATE                  → probability of a transient failure
    """
    # Simulate processing latency
    if settings.mock_delay_max > 0:
        delay = random.uniform(settings.mock_delay_min, settings.mock_delay_max)
        time.sleep(delay)

    # Simulate occasional transient failures (e.g. rate-limit, timeout)
    if settings.mock_error_rate > 0 and random.random() < settings.mock_error_rate:
        raise RetryableError("Mock LLM simulated transient failure")

    payload_lower = {k.lower(): v for k, v in payload.items()}

    if any(k in payload for k in _MOCK_SHIPMENT_KEYS) or "tracking" in payload_lower:
        return {
            "type": "SHIPMENT",
            "vendorId": str(payload.get("vendorId", payload.get("vendor_id", "mock-vendor"))),
            "trackingNumber": str(
                payload.get("trackingNumber", payload.get("tracking_number", "TRK-0000"))
            ),
            "status": str(payload.get("status", "TRANSIT")).upper(),
            "timestamp": payload.get("timestamp", "2024-01-01T00:00:00Z"),
        }

    if any(k in payload for k in _MOCK_INVOICE_KEYS) or "invoice" in str(payload_lower):
        return {
            "type": "INVOICE",
            "vendorId": str(payload.get("vendorId", payload.get("vendor_id", "mock-vendor"))),
            "invoiceId": str(
                payload.get("invoiceId", payload.get("invoice_id", "INV-0000"))
            ),
            "amount": float(payload.get("amount", 0.0)),
            "currency": str(payload.get("currency", "USD")).upper(),
        }

    return {"type": "UNCLASSIFIED", "raw": payload}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def process_with_llm(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Classify and extract structured data from a raw webhook payload.

    Raises RetryableError on failure.
    """
    if settings.llm_mode == "mock":
        return _call_mock(payload)
    return _call_openai(payload)
