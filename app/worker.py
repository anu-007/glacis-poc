"""
RQ worker: event processing pipeline with DB retry and idempotent writes.

Pipeline per event:
    fetch_raw_event
        → [skip if already normalized]          ← idempotent guard
        → call LLM (classify + extract)
        → validate Pydantic schema
        → insert_normalized_event               ← idempotent insert
        → on pipeline failure → insert_dlq_event

The entire pipeline is retried on transient ``OperationalError`` (e.g. DB
restart or lost connection) using exponential back-off.  A fresh Session is
created for every attempt so stale connections are never reused.

Running the worker:
    python -m app.worker
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import repository
from app.config import settings
from app.database import SessionLocal
from app.llm import RetryableError, process_with_llm
from app.schemas import parse_llm_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry-point (called by RQ)
# ---------------------------------------------------------------------------


def process_event(event_id_str: str) -> None:
    """
    Entry point called by RQ.  Drives the processing pipeline and retries
    the whole thing (with a fresh DB session) on transient DB errors.
    """
    event_id = uuid.UUID(event_id_str)

    for attempt in range(1, settings.db_retry_attempts + 1):
        db: Session | None = None
        try:
            db = SessionLocal()
            _run_pipeline(db, event_id)
            return
        except OperationalError as exc:
            if attempt < settings.db_retry_attempts:
                wait = settings.db_retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    "DB unavailable for event %s (attempt %d/%d), retrying in %.1fs: %s",
                    event_id, attempt, settings.db_retry_attempts, wait, exc,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "DB unavailable after %d attempts for event %s; giving up.",
                    settings.db_retry_attempts, event_id,
                )
                raise
        finally:
            if db is not None:
                db.close()


# ---------------------------------------------------------------------------
# Pipeline steps (private)
# ---------------------------------------------------------------------------


def _run_pipeline(db: Session, event_id: uuid.UUID) -> None:
    """
    Orchestrates the three processing steps for one event.

    - Skips silently if the event record is missing or was already normalized.
    - Routes to the DLQ on LLM or schema failures.
    """
    raw = repository.fetch_raw_event(db, event_id)
    if raw is None:
        logger.error("Raw event %s not found; skipping.", event_id)
        return

    if repository.normalized_event_exists(db, event_id):
        logger.info("Event %s already normalized; skipping (idempotent).", event_id)
        return

    payload: dict[str, Any] = raw.payload
    error = _classify_and_save(db, event_id, payload)
    if error:
        repository.insert_dlq_event(db, event_id, payload, error)


def _classify_and_save(
    db: Session,
    event_id: uuid.UUID,
    payload: dict[str, Any],
) -> str | None:
    """
    Call the LLM, validate its output, and persist the normalized record.

    Returns an error string if any step fails, ``None`` on success.
    The caller decides what to do with the error (typically: DLQ).
    """
    try:
        llm_output = process_with_llm(payload)
    except RetryableError as exc:
        return f"LLM failed: {exc}"

    try:
        validated = parse_llm_output(llm_output)
    except Exception as exc:
        return f"Schema validation failed: {exc}"

    event_type = validated.type
    # mode='json' serialises datetime/Enum to JSON-safe primitives
    event_data = validated.model_dump(by_alias=True, mode="json")

    inserted = repository.insert_normalized_event(db, event_id, event_type, event_data)
    if inserted:
        logger.info("Event %s processed as %s.", event_id, event_type)
    return None


# ---------------------------------------------------------------------------
# Worker entry-point  (python -m app.worker)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from rq import Worker

    from app.redis_client import DLQ_QUEUE_NAME, QUEUE_NAME, get_redis

    logging.basicConfig(level=logging.INFO, force=True)
    conn = get_redis()
    worker = Worker([QUEUE_NAME, DLQ_QUEUE_NAME], connection=conn)
    logger.info("Starting RQ worker on queues: %s, %s", QUEUE_NAME, DLQ_QUEUE_NAME)
    worker.work(with_scheduler=False)
