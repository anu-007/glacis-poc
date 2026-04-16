# Glacis Webhook Ingestion Service

A backend service that accepts arbitrary vendor webhook payloads, deduplicates them, classifies and extracts structured data via an LLM, and stores the results — all without blocking the caller.

---

## Architecture

```
Vendor  ──POST /webhook──▶  FastAPI API
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                 Redis        Postgres     RQ Queue
                (dedup)     (raw_events)      │
                                         Worker
                                           │
                                    ┌──────┴──────┐
                                   LLM         Postgres
                              (classify)  (normalized_events
                                           / dlq_events)
```

**Flow per request:**
1. Parse JSON body → SHA-256 hash
2. Redis `SETNX` — reject duplicates immediately (202 `"duplicate"`)
3. Persist raw payload to `raw_events`
4. Enqueue job on RQ → return 202 `"accepted"` immediately
5. Worker picks up job → calls LLM → validates schema → saves to `normalized_events`
6. On failure after retries → moves to `dlq_events`

---

## Project Structure

```
glacis-poc/
├── app/
│   ├── config.py          # All settings (pydantic-settings / .env)
│   ├── database.py        # SQLAlchemy engine, SessionLocal, init_db
│   ├── models.py          # RawEvent, NormalizedEvent (idempotent), DLQEvent
│   ├── repository.py      # Every DB read/write operation — one place, one concern
│   ├── schemas.py         # Pydantic event schemas + parse_llm_output
│   ├── llm.py             # LLM classify+extract (OpenAI or configurable mock)
│   ├── redis_client.py    # Dedup (SETNX) + RQ queue helpers
│   ├── worker.py          # process_event pipeline with DB retry + DLQ routing
│   ├── main.py            # FastAPI app factory
│   └── routes/
│       └── webhooks.py    # POST /api/v1/webhook
├── tests/
│   ├── conftest.py        # SQLite StaticPool + fakeredis fixtures
│   ├── test_schemas.py
│   ├── test_webhooks.py
│   └── test_worker.py     # Includes idempotency + DB-retry tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

### Key design decisions

| Layer | Module | Responsibility |
|---|---|---|
| API | `routes/webhooks.py` | Parse, dedup, persist raw event, enqueue |
| Storage | `repository.py` | All DB reads and writes — callers never touch ORM directly |
| Data models | `models.py` | SQLAlchemy tables; unique constraints enforce idempotency at DB level |
| Processing | `worker.py` | Orchestration only — retry loop, pipeline steps, DLQ routing |
| Classification | `llm.py` | LLM call, prompt, retry, mock |
| Validation | `schemas.py` | Pydantic schemas; acts as the safety net after the LLM |

Adding a new event type (e.g. `PurchaseOrder`) requires changes in exactly three files: `schemas.py` (new model), `llm.py` (prompt + mock), and `repository.py` is unchanged.

---

## Quick Start

### With Docker Compose (recommended)

```bash
cp .env.example .env          # edit OPENAI_API_KEY or leave LLM_MODE=mock
docker compose up --build
```

Services started: `postgres`, `redis`, `api` (port 8000), `worker`.

### Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # configure DATABASE_URL, REDIS_URL, etc.

# Start infrastructure
docker compose up postgres redis -d

# Run migrations (tables are created automatically on startup)
uvicorn app.main:app --reload

# In a separate terminal — start the worker
python -m app.worker
```

---

## Configuration

All settings are read from environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://glacis:glacis@localhost:5432/glacis` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `LLM_MODE` | `mock` | `openai` to call the real API, `mock` for deterministic local mode |
| `OPENAI_API_KEY` | _(empty)_ | Required when `LLM_MODE=openai` |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `DEDUP_TTL` | `3600` | How long (seconds) dedup keys are kept in Redis |
| `MAX_RETRIES` | `3` | LLM call retry attempts before DLQ |
| `JOB_TIMEOUT` | `60` | RQ job timeout in seconds |
| `DB_RETRY_ATTEMPTS` | `3` | How many times the worker retries on transient `OperationalError` |
| `DB_RETRY_DELAY` | `1.0` | Base back-off in seconds (doubles each attempt: 1 s, 2 s, 4 s…) |
| `MOCK_DELAY_MIN` | `0.0` | Minimum mock LLM latency (seconds); set > 0 to simulate slow responses |
| `MOCK_DELAY_MAX` | `0.0` | Maximum mock LLM latency (seconds) |
| `MOCK_ERROR_RATE` | `0.0` | Probability (0–1) that the mock raises a transient error |

---

## API

### `POST /api/v1/webhook`

Accepts any JSON object. Returns immediately.

**Request**
```json
{ "vendorId": "v1", "trackingNumber": "TRK-999", "status": "TRANSIT", "timestamp": "2024-06-01T12:00:00Z" }
```

**Response 202 — accepted**
```json
{ "status": "accepted", "event_id": "<uuid>", "job_id": "<rq-job-id>" }
```

**Response 202 — duplicate**
```json
{ "status": "duplicate" }
```

### `GET /health`
```json
{ "status": "ok" }
```

---

## Event Types (LLM output)

| Type | Required fields |
|---|---|
| `SHIPMENT` | `vendorId`, `trackingNumber`, `status` (`TRANSIT`/`DELIVERED`/`EXCEPTION`), `timestamp` |
| `INVOICE` | `vendorId`, `invoiceId`, `amount` (> 0), `currency` (3-letter ISO) |
| `UNCLASSIFIED` | `raw` (original payload) |

---

## Running Tests

Tests use SQLite (in-memory, `StaticPool`) and `fakeredis` — no running infrastructure needed.

```bash
source .venv/bin/activate
pytest tests/ -v
```

---

## LLM Prompting Strategy

The system prompt is written for **strict, schema-first output** rather than conversational accuracy:

- **Role framing** — the model is told it is a "webhook event classifier for a supply chain platform", giving it the business context needed to make sensible inferences about ambiguous fields (e.g. a `ship_date` field mapping to `timestamp`).
- **Exhaustive examples** — all three output shapes (SHIPMENT, INVOICE, UNCLASSIFIED) are spelled out with their exact field names. This dramatically reduces the chance of the model inventing field aliases.
- **`response_format: json_object`** — the OpenAI JSON mode flag is set so the model is guaranteed to return parseable JSON without markdown fences. `temperature=0` eliminates sampling randomness.
- **Pydantic as the safety net** — the prompt aims for a correct response, but we do not trust it blindly. Every LLM response is run through strict Pydantic validation. A structurally invalid response routes to the DLQ rather than silently corrupting the database.
- **UNCLASSIFIED as an explicit exit** — by making UNCLASSIFIED a first-class type, the model is never forced to guess. Unknown events are stored whole and can be re-processed manually or with a better model later.

---

## Tradeoffs & What We Hacked

| Area | What was done | What a production version needs |
|---|---|---|
| **Migrations** | `Base.metadata.create_all()` on startup | Alembic migrations with versioned, reversible scripts |
| **Worker retries** | Full pipeline retried on `OperationalError` with exponential back-off; RQ's own `rq.Retry` is not wired | Wire `rq.Retry` so RQ itself re-enqueues the job on crashes |
| **Idempotency** | `normalized_events.raw_event_id` unique constraint + `normalized_event_exists` pre-check | ✅ Implemented — safe to re-run the same job multiple times |
| **DB retry** | Worker retries on `OperationalError` with exponential back-off and a fresh session each attempt | ✅ Implemented — survives transient DB restarts |
| **Auth** | No authentication on the webhook endpoint | HMAC signature verification per vendor |
| **Rate limiting** | None | Redis-backed rate limiter per vendor IP / API key |
| **Observability** | Python `logging` only | Structured JSON logs + Prometheus metrics + Sentry for errors |
| **Payload size** | No limit enforced | Reject payloads above a configurable max bytes (e.g. 1 MB) |
| **Config** | Single `Settings` object loaded at module import | Secrets via AWS Secrets Manager / Vault; no plaintext credentials |
| **DLQ replay** | Events land in `dlq_events` with no tooling | Admin endpoint or CLI to list, inspect, and re-enqueue DLQ jobs |
| **Tests** | SQLite + fakeredis; no integration tests | Contract tests against real Postgres + Redis in CI |
| **Mock LLM** | Key-based heuristic with configurable delay/error rate | A real stub server (e.g. WireMock) returning fixture responses |

---

## Production Readiness Checklist

Items marked ✅ are already implemented. The rest are the next priorities before going live.

- ✅ **Idempotent writes** — `normalized_events.raw_event_id` unique constraint + existence pre-check means re-running the same job never creates duplicate records.
- ✅ **DB retry with exponential back-off** — the worker survives transient DB restarts; each retry opens a fresh connection.
- **HMAC webhook authentication** — each vendor provides a secret; every inbound request is verified against a `X-Webhook-Signature` header before any processing.
- **Idempotency beyond dedup TTL** — Redis dedup keys expire after `DEDUP_TTL`. For permanent idempotency on the ingestion path, fall back to a DB lookup on `payload_hash`.
- **Horizontal worker scaling** — RQ workers are stateless; run more replicas behind a shared Redis queue. Use a dedicated `Queue` per event type for independent scaling.
- **Alembic migrations** — replace `create_all()` on startup with versioned, reversible migration scripts.
- **Structured logging + tracing** — emit JSON logs with `event_id`, `job_id`, and `vendor_id` on every line. Add OpenTelemetry spans across API → queue → worker.
- **Circuit breaker on LLM** — use `pybreaker` to stop hammering the LLM API when it is down, fast-failing straight to the DLQ until the API recovers.
- **DLQ tooling** — an authenticated `/admin/dlq` endpoint to list, inspect, and re-enqueue failed events.
- **Payload size guard** — reject requests larger than a configurable limit (e.g. 1 MB) before any DB or Redis interaction.
- **Rate limiting** — Redis-backed per-vendor rate limiter on the ingestion endpoint.
