"""Redis helpers for deduplication and RQ queue management."""

from typing import Optional
import uuid

import redis
from rq import Queue

from app.config import settings

# ---------------------------------------------------------------------------
# Two separate Redis connections
#
# RQ serialises jobs with pickle (binary). If the connection used by Queue /
# Worker has decode_responses=True, Python tries to UTF-8-decode that binary
# data and raises UnicodeDecodeError (byte 0x9c is a pickle marker, not UTF-8).
#
# Solution: keep two connections that share the same Redis server but differ
# in decode_responses:
#   _dedup_conn  – decode_responses=True   – used only for string SETNX keys
#   _rq_conn     – decode_responses=False  – used by RQ Queue / Worker
# ---------------------------------------------------------------------------

_dedup_conn: Optional[redis.Redis] = None  # string-safe, for dedup SETNX
_rq_conn: Optional[redis.Redis] = None     # binary-safe, for RQ pickle data


def _get_dedup_redis() -> redis.Redis:
    """Return the dedup connection (decode_responses=True)."""
    global _dedup_conn
    if _dedup_conn is None:
        _dedup_conn = redis.from_url(settings.redis_url, decode_responses=True)
    return _dedup_conn


def get_redis() -> redis.Redis:
    """Return the RQ connection (decode_responses=False).

    Must stay binary so RQ can read/write pickled job payloads without
    triggering a UnicodeDecodeError.
    """
    global _rq_conn
    if _rq_conn is None:
        _rq_conn = redis.from_url(settings.redis_url, decode_responses=False)
    return _rq_conn


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

DEDUP_PREFIX = "dedup:"


def is_duplicate(payload_hash: str) -> bool:
    """
    Atomically check-and-set a dedup key.

    Returns True if the hash was already present (duplicate),
    False if this is the first time we have seen it (and sets it).
    """
    key = f"{DEDUP_PREFIX}{payload_hash}"
    r = _get_dedup_redis()
    # SETNX + EXPIRE is atomic via SET NX EX
    was_new = r.set(key, "1", nx=True, ex=settings.dedup_ttl)
    return was_new is None  # None → key already existed → duplicate


# ---------------------------------------------------------------------------
# RQ queue helpers
# ---------------------------------------------------------------------------

QUEUE_NAME = "webhook_events"
DLQ_QUEUE_NAME = "webhook_dlq"

_queue: Optional[Queue] = None
_dlq_queue: Optional[Queue] = None


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(QUEUE_NAME, connection=get_redis())
    return _queue


def get_dlq_queue() -> Queue:
    global _dlq_queue
    if _dlq_queue is None:
        _dlq_queue = Queue(DLQ_QUEUE_NAME, connection=get_redis())
    return _dlq_queue


def enqueue_event(event_id: uuid.UUID) -> str:
    """
    Enqueue a raw event for background processing.

    Returns the RQ job id.
    """
    from app.worker import process_event  # local import avoids circular deps

    job = get_queue().enqueue(
        process_event,
        str(event_id),
        job_timeout=settings.job_timeout,
        retry=None,  # retries are handled inside process_event
    )
    return job.id
