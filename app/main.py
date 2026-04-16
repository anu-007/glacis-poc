"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.database import init_db
from app.routes.webhooks import router as webhooks_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Glacis Webhook Ingestion Service",
    description=(
        "Accepts arbitrary vendor webhook payloads, deduplicates them, "
        "stores raw events, and classifies them asynchronously via an LLM."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(webhooks_router, prefix="/api/v1", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
