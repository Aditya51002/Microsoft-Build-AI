from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import redis.asyncio as redis
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.demo import router as demo_router
from api.routes import router as api_router
from api.websocket import router as websocket_router
from config import settings
from core.message_bus import MessageBus
from core.orchestrator import Orchestrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared services and shutdown cleanly."""

    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    message_bus = MessageBus(settings.redis_url)
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    orchestrator = Orchestrator(message_bus, anthropic_client)

    app.state.redis = redis_client
    app.state.message_bus = message_bus
    app.state.anthropic_client = anthropic_client
    app.state.orchestrator = orchestrator

    yield

    await orchestrator.shutdown()
    await redis_client.close()
    await redis_client.connection_pool.disconnect()


app = FastAPI(
    title="ResearchSwarm API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log inbound HTTP requests with timing."""

    logger = logging.getLogger("researchswarm.api")
    start = time.perf_counter()
    response = await call_next(request)
    duration = (time.perf_counter() - start) * 1000
    logger.info("%s %s -> %s (%.2fms)", request.method, request.url.path, response.status_code, duration)
    return response


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions and return JSON responses."""

    logging.getLogger("researchswarm.api").exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"error": str(exc)})


app.include_router(api_router)
app.include_router(demo_router)
app.include_router(websocket_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Compatibility health endpoint for container and judge checks."""

    return {"status": "ok"}
