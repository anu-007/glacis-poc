# Glacis Webhook Ingestion Service

A production-grade backend service that accepts arbitrary vendor webhook payloads, deduplicates them, classifies and extracts structured data via an LLM, and stores the results — all without blocking the caller.

---

## Architecture

```
Vendor  ──POST /webhook──▶  FastAPI API
                                │
                    ┌───────────┼────────────┐
                    ▼           ▼            ▼
                 Redis        Postgres    RQ Queue
               (dedup)     (raw_events)      │
                                             ▼
                                          Worker
                                             │
                                    ┌────────┴────────┐
                                    ▼                 ▼
                                   LLM            Postgres
                              (classify)    (normalized_events
                                              / dlq_events)
```

**Flow per request:**
1. Parse JSON body → compute SHA-256 hash
2. Redis `SETNX` — reject duplicates immediately, return 202 `"duplicate"`
3. Persist raw payload to `raw_events` (DB uniqueness constraint as safety net)
4. Enqueue job on RQ → return 202 `"accepted"` immediately (non-blocking)
5. Worker picks up job → calls LLM → validates Pydantic schema → saves to `normalized_events`
6. On failure after retries → event moves to `dlq_events`

---

## Project Structure

```
glacis-poc/
├── app/
│   ├── config.py          # All settings via pydantic-settings / .env
│   ├── database.py        # SQLAlchemy engine, SessionLocal, init_db
│   ├── models.py          # RawEvent, NormalizedEvent, DLQEvent
│   ├── repository.py      # All DB reads/writes — one place, one concern
│   ├── schemas.py         # Pydantic event schemas + parse_llm_output
│   ├── llm.py             # LLM classify+extract (OpenAI or mock)
│   ├── redis_client.py    # Dedup (SETNX) + RQ queue helpers (two connections)
│   ├── worker.py          # process_event pipeline with DB retry + DLQ routing
│   ├── main.py            # FastAPI app factory + lifespan
│   └── routes/
│       └── webhooks.py    # POST /api/v1/webhook
├── tests/
│   ├── conftest.py        # SQLite StaticPool + fakeredis fixtures
│   ├── test_schemas.py
│   ├── test_webhooks.py
│   └── test_worker.py     # Idempotency + DB-retry tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

### Key Design Decisions

| Layer | Module | Responsibility |
|---|---|---|
| API | `routes/webhooks.py` | Parse, dedup, persist raw event, enqueue |
| Storage | `repository.py` | All DB reads and writes — callers never touch ORM directly |
| Data models | `models.py` | SQLAlchemy tables; unique constraints enforce idempotency at DB level |
| Processing | `worker.py` | Orchestration only — retry loop, pipeline steps, DLQ routing |
| Classification | `llm.py` | LLM call, prompt, retry, mock |
| Validation | `schemas.py` | Pydantic schemas; safety net after the LLM |

Adding a new event type (e.g. `PurchaseOrder`) requires changes in exactly two files: `schemas.py` (new model + `parse_llm_output`) and `llm.py` (prompt + mock). `repository.py` is unchanged.

---

## Quick Start

### With Docker Compose (recommended)

> **Important:** When running with Docker Compose, use service names (`postgres`, `redis`) — not `localhost` — in your `.env` file. Inside Docker, `localhost` refers to the container itself, not other services.

```bash
cp .env.example .env
# Edit .env:
#   DATABASE_URL=postgresql://glacis:glacis@postgres:5432/glacis
#   REDIS_URL=redis://redis:6379/0
#   Set OPENAI_API_KEY if using LLM_MODE=openai

docker compose up --build
```

Services started: `postgres` (5432), `redis` (6379), `api` (8000), `worker`.

### Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env:
#   DATABASE_URL=postgresql://glacis:glacis@localhost:5432/glacis
#   REDIS_URL=redis://localhost:6379/0

# Start infrastructure only
docker compose up postgres redis -d

# Run the API (tables are created automatically on startup)
uvicorn app.main:app --reload

# In a separate terminal — start the worker
python -m app.worker
```

---

## Configuration

All settings are read from environment variables or a `.env` file (via `pydantic-settings`).

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://glacis:glacis@localhost:5432/glacis` | PostgreSQL connection string. Use `@postgres:5432` inside Docker Compose. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string. Use `redis://redis:6379/0` inside Docker Compose. |
| `LLM_MODE` | `mock` | `openai` to call the real API, `mock` for deterministic local responses |
| `OPENAI_API_KEY` | _(empty)_ | Required when `LLM_MODE=openai` |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `DEDUP_TTL` | `3600` | How long (seconds) dedup keys are kept in Redis |
| `MAX_RETRIES` | `3` | LLM call retry attempts before routing to DLQ |
| `JOB_TIMEOUT` | `60` | RQ job timeout in seconds |
| `DB_RETRY_ATTEMPTS` | `3` | Worker retries on transient `OperationalError` |
| `DB_RETRY_DELAY` | `1.0` | Base back-off in seconds (doubles each attempt: 1 s → 2 s → 4 s) |
| `MOCK_DELAY_MIN` | `0.0` | Min mock LLM latency (seconds) |
| `MOCK_DELAY_MAX` | `0.0` | Max mock LLM latency (seconds) |
| `MOCK_ERROR_RATE` | `0.0` | Probability (0–1) that the mock raises a transient error |

---

## API Reference

### `POST /api/v1/webhook`

Accepts any JSON object. Returns immediately (non-blocking).

**Request**
```json
{ "vendorId": "v1", "trackingNumber": "TRK-999", "status": "TRANSIT", "timestamp": "2024-06-01T12:00:00Z" }
```

**202 — accepted**
```json
{ "status": "accepted", "event_id": "<uuid>", "job_id": "<rq-job-id>" }
```

**202 — duplicate**
```json
{ "status": "duplicate" }
```

**400** — body is not a valid JSON object
**500** — DB or Redis failure

---

### `GET /health`
```json
{ "status": "ok" }
```

**Interactive docs:** http://localhost:8000/docs (Swagger UI)

---

## Event Types

The LLM classifies each payload into one of three types:

| Type | Required fields |
|---|---|
| `SHIPMENT` | `vendorId`, `trackingNumber`, `status` (`TRANSIT` / `DELIVERED` / `EXCEPTION`), `timestamp` (ISO 8601) |
| `INVOICE` | `vendorId`, `invoiceId`, `amount` (positive number), `currency` (3-letter ISO code) |
| `UNCLASSIFIED` | `raw` (original payload stored as-is for manual review) |

---

## Running Tests

Tests use **SQLite in-memory** (`StaticPool`) and **fakeredis** — no running PostgreSQL or Redis needed.

```bash
source .venv/bin/activate
pytest tests/ -v
```

**Test coverage:**
- ✅ Successful webhook ingestion (202 accepted)
- ✅ Duplicate payload rejection
- ✅ Raw event stored in DB
- ✅ Invalid JSON → 400
- ✅ JSON array (not object) → 400
- ✅ Redis/enqueue failure → 500
- ✅ Health check
- ✅ LLM failure routes to DLQ
- ✅ Schema validation failure routes to DLQ
- ✅ Idempotent processing (same job run twice)
- ✅ DB retry on transient OperationalError

---

## Redis Key Structure

For each processed event, two Redis keys are created:

| Key | Description | Expires |
|---|---|---|
| `dedup:<sha256-hash>` | Dedup lock — prevents re-processing the same payload | After `DEDUP_TTL` (default 1 hour) |
| `rq:job:<job-id>` | RQ job metadata (pickled, binary) | After job completes |

> **Note:** Two separate Redis connections are used internally — `decode_responses=True` for dedup string keys, and `decode_responses=False` for RQ's binary pickle data. Using a single connection for both causes a `UnicodeDecodeError` (byte `0x9c` is a pickle marker, not valid UTF-8).

To delete an event's Redis keys (e.g. to allow re-submission of the same payload):

```bash
# In Docker Desktop → redis container → Exec tab, or via redis-cli:
KEYS dedup:*                              # find dedup lock for a payload
DEL dedup:<sha256-hash>                   # remove dedup lock
DEL rq:job:<job-id>                       # remove RQ job record
LRANGE rq:queue:webhook_events 0 -1      # view pending jobs
```

## Debugging the Database

```bash
docker compose exec postgres psql -U glacis -d glacis
```

```sql
-- All raw events received
SELECT id, payload_hash, created_at FROM raw_events ORDER BY created_at DESC;

-- Normalized (LLM-classified) events
SELECT id, raw_event_id, type, data FROM normalized_events ORDER BY created_at DESC;

-- Dead-letter queue — failed events
SELECT id, raw_event_id, error, created_at FROM dlq_events ORDER BY created_at DESC;

-- Row counts across all tables
SELECT 'raw_events' AS tbl, COUNT(*) FROM raw_events
UNION ALL SELECT 'normalized_events', COUNT(*) FROM normalized_events
UNION ALL SELECT 'dlq_events', COUNT(*) FROM dlq_events;
```

---

## LLM Prompting Strategy

The system prompt is written for **strict, schema-first output**:

- **Role framing** — the model is told it is a "strict JSON transformer for webhook events in a supply chain platform", giving it the business context to make sensible inferences about ambiguous fields.
- **Exhaustive schemas** — all three output shapes (SHIPMENT, INVOICE, UNCLASSIFIED) are spelled out with exact field names and types, reducing the chance of the model inventing field aliases.
- **`response_format: json_object`** — OpenAI JSON mode guarantees parseable JSON without markdown fences. `temperature=0` eliminates sampling randomness.
- **Pydantic as the safety net** — every LLM response is run through strict Pydantic validation. A structurally invalid response routes to the DLQ rather than silently corrupting the database.
- **UNCLASSIFIED as an explicit exit** — by making UNCLASSIFIED a first-class type, the model is never forced to guess. Unknown events are stored whole and can be re-processed manually or with a better model later.

---

## Known Failure Points & Production Fixes

### 🔴 Critical

#### 1. Redis Is a Single Point of Failure
If Redis goes down, the webhook endpoint immediately returns `500` — both `is_duplicate()` and `enqueue_event()` depend on it.

**Fix:** Use **Redis Sentinel** or **Redis Cluster** (AWS ElastiCache, Redis Cloud) for HA. Make dedup fail-open: if Redis is unreachable, skip the check and let the DB `unique constraint` on `payload_hash` catch duplicates. Add connection timeouts and pool limits.

#### 2. No Database Connection Pool Limits
SQLAlchemy's default pool size is 5 connections. Under load, this causes "connection pool exhausted" errors.

**Fix:**
```python
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,       # steady-state connections
    max_overflow=20,    # burst connections
    pool_timeout=30,    # wait before raising error
    pool_recycle=1800,  # recycle connections every 30 min
)
```
Add **PgBouncer** or **RDS Proxy** at the infrastructure level.

#### 3. Single Worker — No Horizontal Scaling
One worker process. If it crashes or falls behind, jobs pile up indefinitely with no alerting.

**Fix:** Run multiple replicas: `docker compose up --scale worker=4`. Monitor queue depth and alert when `LLEN rq:queue:webhook_events` exceeds a threshold using **RQ Dashboard** or Prometheus + `rq-exporter`.

#### 4. DLQ Events Are Never Retried or Alerted On
Failed events land in `dlq_events` and are silently forgotten — no retry, no alert, no tooling.

**Fix:** Add a scheduled job to periodically re-enqueue DLQ events. Send alerts (Slack, PagerDuty) when a DLQ insert occurs. Add an authenticated admin endpoint (`GET /admin/dlq`) to list and replay failed events.

---

### 🟡 High Priority

#### 5. OpenAI API Key Not Validated at Startup
If `LLM_MODE=openai` but `OPENAI_API_KEY` is empty, every background job silently fails to the DLQ.

**Fix:** Add a startup check that raises immediately if the key is missing. Use a secrets manager (AWS Secrets Manager, HashiCorp Vault, Doppler) instead of a `.env` file for credentials.

#### 6. OpenAI Timeout Is Too Short (5 seconds)
OpenAI can take longer than 5s under load, causing unnecessary retries and DLQ entries.

**Fix:** Increase timeout to 30s and make it configurable via `settings`. Add exponential backoff with jitter between retries instead of retrying immediately.

#### 7. No Request Body Size Limit
Callers can send arbitrarily large JSON payloads, causing slow responses, high LLM costs, and potential OOM issues.

**Fix:** Add a body size middleware before any DB or Redis interaction:
```python
@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    if int(request.headers.get("content-length", 0)) > 64 * 1024:  # 64 KB
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})
    return await call_next(request)
```

#### 8. No Authentication on the Webhook Endpoint
The endpoint is completely open — any caller can flood it with arbitrary data.

**Fix:** Add **HMAC signature verification** per vendor (standard practice: Stripe, GitHub, etc.), or enforce an **API key header** per vendor. Rate-limit by IP/key at the reverse proxy (nginx, Caddy) or via `slowapi`.

---

### 🟡 Medium Priority

#### 9. `--reload` Flag Used in Production
`docker-compose.yml` runs uvicorn with `--reload`, which watches for file changes and is intended for development only.

**Fix:**
```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

#### 10. Short Dedup TTL Can Allow Re-submission
The dedup key expires after `DEDUP_TTL` (default 1 hour). A vendor retrying the exact same payload after TTL expiry creates a duplicate `normalized_event`.

**Fix:** Increase `DEDUP_TTL` to 24h or 7d. The DB `unique constraint` on `payload_hash` in `raw_events` is the permanent safety net.

---

## Tradeoffs & What Was Hacked

| Area | Current State | Production Target |
|---|---|---|
| **Migrations** | `Base.metadata.create_all()` on startup | Alembic — versioned, reversible migration scripts |
| **Worker retries** | Exponential back-off on `OperationalError` only | Wire `rq.Retry` so RQ re-enqueues the job on any crash |
| **Idempotency** | ✅ Unique constraint + existence pre-check | Already production-ready |
| **DB retry** | ✅ Exponential back-off with fresh session per attempt | Already production-ready |
| **Redis connections** | ✅ Separate connections for dedup (text) and RQ (binary) | Already production-ready |
| **Auth** | None | HMAC signature verification per vendor |
| **Rate limiting** | None | Redis-backed per-vendor rate limiter |
| **Observability** | Python `logging` only | Structured JSON logs + Prometheus metrics + Sentry |
| **Payload size** | No limit | Reject above a configurable max (e.g. 64 KB) |
| **Secrets** | `.env` file | AWS Secrets Manager / Vault — no plaintext credentials |
| **DLQ replay** | Events stored, no tooling | Admin endpoint or CLI to list, inspect, and re-enqueue |
| **Tests** | SQLite + fakeredis unit tests | Integration tests against real Postgres + Redis in CI |
| **LLM circuit breaker** | None | `pybreaker` — fast-fail on a down LLM API, stop retrying |

---

## Production Readiness Checklist

- ✅ **Idempotent writes** — unique constraint + existence pre-check; re-running the same job never creates duplicates
- ✅ **DB retry with exponential back-off** — worker survives transient DB restarts; fresh connection per attempt
- ✅ **Separate Redis connections** — `decode_responses=True` for dedup, `decode_responses=False` for RQ pickle data
- ✅ **DLQ routing** — all pipeline failures route to `dlq_events` with full error context
- ⬜ **Redis HA** — Sentinel or Cluster for high availability
- ⬜ **DB connection pool limits** — configure `pool_size`, `max_overflow`; add PgBouncer / RDS Proxy
- ⬜ **Horizontal worker scaling** — multiple replicas + queue depth monitoring and alerting
- ⬜ **DLQ alerting and replay** — alerts on DLQ writes + admin endpoint to list and re-enqueue
- ⬜ **Startup validation** — raise on missing `OPENAI_API_KEY` when `LLM_MODE=openai`
- ⬜ **Webhook authentication** — HMAC signature verification per vendor
- ⬜ **Rate limiting** — per-vendor rate limiter on the ingestion endpoint
- ⬜ **Request size guard** — reject oversized payloads before any DB or Redis interaction
- ⬜ **Remove `--reload`** — use `--workers N` in the production uvicorn command
- ⬜ **Alembic migrations** — replace `create_all()` with versioned, reversible migration scripts
- ⬜ **Structured logging** — JSON logs with `event_id`, `job_id`, `vendor_id` on every line; OpenTelemetry tracing
- ⬜ **LLM circuit breaker** — `pybreaker` to fast-fail on a down LLM API instead of retrying to exhaustion
- ⬜ **Secrets management** — no plaintext credentials in `.env`; use Vault / AWS Secrets Manager
