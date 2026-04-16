"""
Shared pytest fixtures.

Uses:
  - SQLite in-memory database with StaticPool (single shared connection)
  - fakeredis (no Redis needed)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import fakeredis

from app.database import Base, get_db
from app.main import app
import app.redis_client as rc_module


# ---------------------------------------------------------------------------
# In-memory SQLite engine (StaticPool = single connection shared by all)
# ---------------------------------------------------------------------------

SQLITE_URL = "sqlite://"


@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine(
        SQLITE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import app.models  # noqa: F401 — populate Base.metadata
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    TestingSession = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def db_session_factory(db_engine):
    """A sessionmaker bound to the test SQLite engine.

    Use this to patch app.worker.SessionLocal so the worker creates its own
    sessions (which it can safely close) without affecting the test's own
    db_session.
    """
    return sessionmaker(bind=db_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Replace both module-level Redis connections with fakeredis.

    _dedup_conn uses decode_responses=True  (string SETNX keys).
    _rq_conn    uses decode_responses=False (binary RQ pickle data).
    Both share the same FakeServer so keys are visible across connections.
    """
    server = fakeredis.FakeServer()
    fake_dedup_conn = fakeredis.FakeRedis(server=server, decode_responses=True)
    fake_rq_conn = fakeredis.FakeRedis(server=server, decode_responses=False)

    monkeypatch.setattr(rc_module, "_dedup_conn", fake_dedup_conn)
    monkeypatch.setattr(rc_module, "_rq_conn", fake_rq_conn)
    monkeypatch.setattr(rc_module, "_queue", None)
    monkeypatch.setattr(rc_module, "_dlq_queue", None)
    return fake_dedup_conn


# ---------------------------------------------------------------------------
# FastAPI TestClient with DB override
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def client(db_session, monkeypatch):
    def _override_get_db():
        yield db_session

    # Prevent lifespan from connecting to the real PostgreSQL database.
    monkeypatch.setattr("app.main.init_db", lambda: None)

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()
