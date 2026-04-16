"""
POST /webhook — the main ingestion endpoint.

Flow:
  1. Parse JSON body
  2. SHA-256 hash the payload for dedup
  3. Redis SETNX — reject duplicates immediately (sub-millisecond)
  4. Persist raw event to PostgreSQL via repository
  5. Enqueue background job
  6. Return 202 Accepted
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import repository
from app.database import get_db
from app.redis_client import enqueue_event, is_duplicate
from app.schemas import WebhookAcceptedResponse

logger = logging.getLogger(__name__)
router = APIRouter()


def _hash_payload(payload: Any) -> str:
    """SHA-256 of the canonical (sorted-key) JSON form of the payload."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAcceptedResponse,
    summary="Ingest a vendor webhook payload",
)
async def ingest_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> WebhookAcceptedResponse:
    # 1. Parse body
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body must be valid JSON.")

    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload must be a JSON object.")

    # 2. Dedup — Redis SETNX (fast path, avoids a DB round-trip)
    payload_hash = _hash_payload(payload)
    if is_duplicate(payload_hash):
        logger.info("Duplicate payload (hash=%s); ignoring.", payload_hash[:12])
        return WebhookAcceptedResponse(status="duplicate")

    # 3. Persist raw event
    try:
        raw_event = repository.insert_raw_event(db, payload, payload_hash)
    except IntegrityError:
        # Race condition: a concurrent request won the hash uniqueness race.
        db.rollback()
        logger.info("DB uniqueness caught duplicate (hash=%s).", payload_hash[:12])
        return WebhookAcceptedResponse(status="duplicate")
    except Exception as exc:
        logger.exception("Failed to save raw event: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to persist event.")

    # 4. Enqueue background job
    # Raw event is intentionally NOT rolled back on queue failure — it can be replayed.
    try:
        job_id = enqueue_event(raw_event.id)
    except Exception as exc:
        logger.exception("Failed to enqueue event %s: %s", raw_event.id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Event stored but could not be queued.")

    logger.info("Accepted event %s (job=%s).", raw_event.id, job_id)
    return WebhookAcceptedResponse(
        status="accepted",
        event_id=str(raw_event.id),
        job_id=job_id,
    )
