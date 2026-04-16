import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, String, Text, DateTime, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# JSON maps to JSONB in PostgreSQL via SQLAlchemy's native_enum/JSON handling;
# it also works transparently with SQLite (used in tests).


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawEvent(Base):
    """Stores the raw, unmodified webhook payload."""

    __tablename__ = "raw_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=False), primary_key=True, default=uuid.uuid4
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (UniqueConstraint("payload_hash", name="uq_raw_events_payload_hash"),)


class NormalizedEvent(Base):
    """Stores the LLM-classified and schema-validated event."""

    __tablename__ = "normalized_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=False), primary_key=True, default=uuid.uuid4
    )
    raw_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=False), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # One normalized record per raw event — enforces idempotent writes.
    __table_args__ = (UniqueConstraint("raw_event_id", name="uq_normalized_events_raw_event_id"),)


class DLQEvent(Base):
    """Dead Letter Queue — stores events that could not be processed after max retries."""

    __tablename__ = "dlq_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=False), primary_key=True, default=uuid.uuid4
    )
    raw_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=False), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
