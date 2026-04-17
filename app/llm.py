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
You are a strict JSON transformer for webhook events in a supply chain platform.

Your task:
1. Classify the input JSON into exactly one of: SHIPMENT, INVOICE, UNCLASSIFIED
2. Extract only the required fields for that type
3. Return a single valid JSON object that strictly matches one schema below

CRITICAL RULES:
- Output MUST be valid JSON (no text, no markdown, no comments)
- Do NOT include extra fields
- Do NOT rename fields
- Do NOT infer values unless clearly present
- If required fields are missing or ambiguous, return UNCLASSIFIED
- Enums MUST match exactly (case-sensitive)
- Timestamps MUST be ISO 8601 format if present
- Amount MUST be a number (not string)

---

SHIPMENT schema:
{
  "type": "SHIPMENT",
  "vendorId": string,
  "trackingNumber": string,
  "status": "TRANSIT" | "DELIVERED" | "EXCEPTION",
  "timestamp": string
}

Rules:
- status must map strictly to one of the allowed values
- If status cannot be confidently mapped → UNCLASSIFIED

---

INVOICE schema:
{
  "type": "INVOICE",
  "vendorId": string,
  "invoiceId": string,
  "amount": number,
  "currency": string
}

Rules:
- currency must be a 3-letter ISO code (e.g., USD, EUR, INR)
- amount must be numeric (not string)

---

UNCLASSIFIED schema:
{
  "type": "UNCLASSIFIED",
  "raw": <original input JSON>
}

---

Important:
- Prefer UNCLASSIFIED over incorrect classification
- Do not hallucinate missing fields
- Do not partially fill schemas

Return ONLY the JSON object.
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
