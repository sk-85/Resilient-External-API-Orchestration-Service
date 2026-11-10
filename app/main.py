"""Application factory.

Run with:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request

from app import __version__, metrics
from app.config import Settings, get_settings
from app.database import Database
from app.logging_config import configure_logging, correlation_id_var
from app.routes import ops, v1
from app.services.circuit_breaker import CircuitBreaker, CircuitState
from app.services.external_client import ExternalClient
from app.services.orchestrator import Orchestrator
from app.services.retry import RetryPolicy
from app.tracing import setup_tracing

logger = logging.getLogger(__name__)

# Only accept sane inbound correlation IDs; otherwise generate our own.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_QUIET_ROUTES = {"/health/live", "/health/ready", "/metrics"}


def create_app(
    settings: Settings | None = None, *, http_client: httpx.AsyncClient | None = None
) -> FastAPI:
    """Build the app. ``settings`` and ``http_client`` can be injected (used by the tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    upstream_name = urlparse(settings.upstream_base_url).netloc or "upstream"

    def on_breaker_change(name: str, old: CircuitState, new: CircuitState) -> None:
        metrics.CIRCUIT_STATE.labels(upstream=name).set(metrics.CIRCUIT_STATE_VALUES[new.value])
        metrics.CIRCUIT_TRANSITIONS.labels(upstream=name, to_state=new.value).inc()
        logger.warning(
            "circuit breaker state changed",
            extra={"upstream": name, "from_state": old.value, "to_state": new.value},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        await db.create_all()

        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            follow_redirects=False,  # never let the upstream bounce us elsewhere
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

        breaker = CircuitBreaker(
            upstream_name,
            failure_threshold=settings.breaker_failure_threshold,
            recovery_timeout=settings.breaker_recovery_seconds,
            half_open_max_calls=settings.breaker_half_open_max_calls,
            on_state_change=on_breaker_change,
        )
        metrics.CIRCUIT_STATE.labels(upstream=upstream_name).set(0)

        app.state.db = db
        app.state.breaker = breaker
        app.state.orchestrator = Orchestrator(
            db=db,
            client=ExternalClient(
                client,
                settings.upstream_base_url,
                settings.request_timeout_seconds,
                settings.max_response_bytes,
            ),
            breaker=breaker,
            policy=RetryPolicy(
                max_attempts=settings.retry_max_attempts,
                base_delay=settings.retry_base_delay_seconds,
                max_delay=settings.retry_max_delay_seconds,
                jitter=settings.retry_jitter,
            ),
            total_timeout=settings.total_timeout_seconds,
        )
        logger.info(
            "service started",
            extra={"upstream": settings.upstream_base_url, "auth_mode": settings.auth_mode},
        )
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()
            await db.dispose()

    app = FastAPI(
        title="Resilient External API Orchestration Service",
        description="Calls external APIs with retries, backoff, circuit breaking and a full audit trail.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings  # available to auth before the lifespan runs

    @app.middleware("http")
    async def observability(request: Request, call_next):  # type: ignore[no-untyped-def]
        inbound = request.headers.get("x-request-id", "")
        correlation_id = inbound if _VALID_REQUEST_ID.match(inbound) else uuid.uuid4().hex
        token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = correlation_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(request.scope.get("route"), "path", "unmatched")
            metrics.HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
            metrics.HTTP_LATENCY.labels(request.method, route).observe(elapsed)
            if route not in _QUIET_ROUTES:
                logger.info(
                    "request completed",
                    extra={
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "http_route": route,
                        "http_status": status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )
            correlation_id_var.reset(token)

    app.include_router(v1)
    app.include_router(ops)
    setup_tracing(app, settings)
    return app
