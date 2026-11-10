"""The orchestration flow: persist -> call upstream (retry + breaker) -> persist outcome."""

from __future__ import annotations

import logging
import time
import uuid

from opentelemetry import trace

from app import metrics
from app.database import Database
from app.models import AttemptLog, AuditEvent, Orchestration, utcnow
from app.schemas import OrchestrationRequest
from app.services.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.services.external_client import ExternalAPIError, ExternalClient, ExternalResponse
from app.services.retry import Attempt, RetryPolicy, retry_async

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("app.orchestrator")

# Retrying these twice can repeat a side effect, so they are attempted once by default.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})


def _counts_as_failure(exc: Exception) -> bool:
    """Does this error mean the upstream is unhealthy (and so count toward opening the circuit)?

    A definitive 4xx (e.g. 404) means the upstream answered correctly, so it does not.
    Timeouts, connection errors, 5xx and 429 do. Unknown errors are treated as failures.
    """
    return not (isinstance(exc, ExternalAPIError) and not exc.retryable)


def _outcome(attempt: Attempt[ExternalResponse]) -> str:
    error = attempt.error
    if error is None:
        return "success"
    if isinstance(error, CircuitOpenError):
        return "circuit_open"
    if isinstance(error, ExternalAPIError) and not error.retryable:
        return "client_error"
    return "retryable_error"


def _status_of(attempt: Attempt[ExternalResponse]) -> int | None:
    """HTTP status the upstream answered with on this attempt (None for timeouts etc.)."""
    if attempt.result is not None:
        return attempt.result.status_code
    return getattr(attempt.error, "status_code", None)


def _attempt_row(orchestration_id: str, attempt: Attempt[ExternalResponse]) -> AttemptLog:
    error = attempt.error
    return AttemptLog(
        orchestration_id=orchestration_id,
        attempt_number=attempt.number,
        started_at=attempt.started_at,
        duration_ms=round(attempt.duration_ms, 2),
        success=error is None,
        status_code=_status_of(attempt),
        retryable=getattr(error, "retryable", None),
        error=f"{type(error).__name__}: {error}" if error else None,
    )


class Orchestrator:
    def __init__(
        self,
        *,
        db: Database,
        client: ExternalClient,
        breaker: CircuitBreaker,
        policy: RetryPolicy,
        total_timeout: float,
    ) -> None:
        self._db = db
        self._client = client
        self._breaker = breaker
        self._policy = policy
        self._total_timeout = total_timeout

    async def execute(
        self, req: OrchestrationRequest, *, correlation_id: str, actor: str
    ) -> tuple[Orchestration, float | None]:
        """Run one orchestration. Returns the stored record and, if the circuit was
        open, the number of seconds after which the caller may try again."""
        started = time.perf_counter()
        row = Orchestration(
            id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            actor=actor,
            method=req.method,
            path=req.path,
            status="running",
            attempts=0,
            created_at=utcnow(),
        )

        async with self._db.session_factory() as session:
            # Persist first so the request is auditable even if the process dies mid-call.
            session.add(row)
            session.add(
                AuditEvent(
                    orchestration_id=row.id,
                    event="created",
                    actor=actor,
                    detail={"method": req.method, "path": req.path},
                )
            )
            await session.commit()

            attempts: list[Attempt[ExternalResponse]] = []
            retry_after: float | None = None
            response: ExternalResponse | None = None
            error_text: str | None = None

            can_retry = req.method in IDEMPOTENT_METHODS or req.allow_unsafe_retries
            policy = self._policy if can_retry else RetryPolicy(max_attempts=1)

            def is_retryable(exc: Exception) -> bool:
                return isinstance(exc, ExternalAPIError) and exc.retryable

            def on_attempt(attempt: Attempt[ExternalResponse]) -> None:
                attempts.append(attempt)
                metrics.EXTERNAL_CALLS.labels(outcome=_outcome(attempt)).inc()
                metrics.EXTERNAL_LATENCY.observe(attempt.duration_ms / 1000)

            def on_retry(attempt_no: int, delay: float, exc: Exception) -> None:
                metrics.RETRIES.inc()
                logger.warning(
                    "upstream attempt failed, retrying",
                    extra={
                        "orchestration_id": row.id,
                        "attempt": attempt_no,
                        "retry_in_seconds": round(delay, 3),
                        "error": str(exc),
                    },
                )

            async def attempt_once(_number: int) -> ExternalResponse:
                return await self._breaker.call(
                    self._client.request,
                    req.method,
                    req.path,
                    params=req.params,
                    json=req.payload,
                    headers={"X-Request-ID": correlation_id},
                    is_failure=_counts_as_failure,
                )

            with tracer.start_as_current_span("orchestration.execute") as span:
                span.set_attribute("orchestration.id", row.id)
                span.set_attribute("orchestration.method", req.method)
                span.set_attribute("orchestration.path", req.path)
                try:
                    response = await retry_async(
                        attempt_once,
                        policy=policy,
                        is_retryable=is_retryable,
                        on_attempt=on_attempt,
                        on_retry=on_retry,
                        deadline=time.monotonic() + self._total_timeout,
                    )
                    row.status = "succeeded"
                except CircuitOpenError as exc:
                    row.status = "circuit_open"
                    error_text = str(exc)
                    retry_after = exc.retry_in
                except ExternalAPIError as exc:
                    row.status = "failed"
                    error_text = str(exc)
                except Exception as exc:
                    row.status = "error"
                    error_text = f"internal error: {type(exc).__name__}"
                    logger.exception(
                        "unexpected error during orchestration", extra={"orchestration_id": row.id}
                    )

                span.set_attribute("orchestration.status", row.status)
                span.set_attribute("orchestration.attempts", len(attempts))

            # Last status code we actually heard from the upstream, if any.
            upstream_status = next(
                (s for a in reversed(attempts) if (s := _status_of(a)) is not None), None
            )

            row.attempts = len(attempts)
            row.upstream_status = upstream_status
            row.response_body = response.body if response else None
            row.error = error_text
            row.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            row.completed_at = utcnow()

            session.add_all(_attempt_row(row.id, a) for a in attempts)
            session.add(
                AuditEvent(
                    orchestration_id=row.id,
                    event=row.status,
                    actor=actor,
                    detail={
                        "attempts": row.attempts,
                        "upstream_status": upstream_status,
                        "error": error_text,
                    },
                )
            )
            await session.commit()

        metrics.ORCHESTRATIONS.labels(status=row.status).inc()
        logger.info(
            "orchestration finished",
            extra={
                "orchestration_id": row.id,
                "status": row.status,
                "attempts": row.attempts,
                "upstream_status": upstream_status,
                "duration_ms": row.duration_ms,
            },
        )
        return row, retry_after
