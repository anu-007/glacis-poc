"""
Database repository — every DB read/write operation lives here.

Design rules:
- Every function accepts an open ``Session`` and performs exactly one concern.
- Session lifecycle (open / close / retry) is the caller's responsibility.
- ``insert_normalized_event`` is intentionally idempotent: a duplicate write
  for the same ``raw_event_id`` is silently absorbed and returns ``False``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DLQEvent, NormalizedEvent, RawEvent

logger = logging.getLogger(__name__)


def fetch_raw_event(db: Session, event_id: uuid.UUID) -> RawEvent | None:
    """Load a raw event by primary key. Returns ``None`` if not found."""
    return db.get(RawEvent, event_id)


def normalized_event_exists(db: Session, raw_event_id: uuid.UUID) -> bool:
    """Return ``True`` if a normalized record already exists for this raw event."""
    return (
        db.query(NormalizedEvent)
        .filter_by(raw_event_id=raw_event_id)
        .first()
    ) is not None


def insert_raw_event(
    db: Session,
    payload: dict[str, Any],
    payload_hash: str,
) -> RawEvent:
    """
    Persist a raw webhook payload and return the saved record.

    Callers should handle ``IntegrityError`` for hash-based dedup
    (raised when the same payload hash is inserted twice).
    """
    event = RawEvent(payload=payload, payload_hash=payload_hash)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def insert_normalized_event(
    db: Session,
    raw_event_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
) -> bool:
    """
    Persist a normalized event record.

    Idempotent: if a record for ``raw_event_id`` already exists (enforced by
    a unique constraint), the insert is rolled back and ``False`` is returned.
    Returns ``True`` on a successful new insert.
    """
    record = NormalizedEvent(raw_event_id=raw_event_id, type=event_type, data=data)
    db.add(record)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        logger.info("Normalized event for %s already exists; skipping.", raw_event_id)
        return False


def insert_dlq_event(
    db: Session,
    raw_event_id: uuid.UUID | None,
    payload: dict[str, Any],
    error: str,
) -> None:
    """Append a failure record to the dead-letter queue."""
    record = DLQEvent(raw_event_id=raw_event_id, payload=payload, error=error)
    db.add(record)
    db.commit()
    logger.error("Event %s moved to DLQ: %s", raw_event_id, error)
