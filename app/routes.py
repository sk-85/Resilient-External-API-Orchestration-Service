"""HTTP endpoints."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from app.auth import get_principal
from app.logging_config import correlation_id_var
from app.models import AttemptLog, AuditEvent, Orchestration
from app.schemas import (
    AttemptOut,
    AuditOut,
    OrchestrationDetail,
    OrchestrationOut,
    OrchestrationRequest,
    Status,
)

# How an orchestration's final status maps onto our HTTP response.
HTTP_STATUS_FOR = {
    "succeeded": 200,
    "failed": 502,  # upstream failed after retries, or rejected the request
    "circuit_open": 503,  # we refused to call a struggling upstream
    "error": 500,  # bug on our side
}

v1 = APIRouter(prefix="/v1", tags=["orchestrations"])
ops = APIRouter(tags=["operations"])


@v1.post(
    "/orchestrations",
    response_model=OrchestrationOut,
    summary="Call the external API with retries and circuit breaking",
    responses={
        502: {"model": OrchestrationOut, "description": "Upstream failed after retries."},
        503: {"model": OrchestrationOut, "description": "Circuit open; see Retry-After."},
    },
)
async def create_orchestration(
    body: OrchestrationRequest,
    request: Request,
    response: Response,
    principal: str = Depends(get_principal),
) -> OrchestrationOut:
    row, retry_after = await request.app.state.orchestrator.execute(
        body, correlation_id=correlation_id_var.get(), actor=principal
    )
    response.status_code = HTTP_STATUS_FOR.get(row.status, 500)
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return OrchestrationOut.from_row(row)


@v1.get("/orchestrations", response_model=list[OrchestrationOut], summary="List recent orchestrations")
async def list_orchestrations(
    request: Request,
    status_filter: Status | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _principal: str = Depends(get_principal),
) -> list[OrchestrationOut]:
    stmt = select(Orchestration).order_by(Orchestration.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        stmt = stmt.where(Orchestration.status == status_filter)
    async with request.app.state.db.session_factory() as session:
        rows = (await session.scalars(stmt)).all()
    return [OrchestrationOut.from_row(r) for r in rows]


@v1.get(
    "/orchestrations/{orchestration_id}",
    response_model=OrchestrationDetail,
    summary="Get one orchestration with its attempt log and audit trail",
)
async def get_orchestration(
    orchestration_id: str,
    request: Request,
    _principal: str = Depends(get_principal),
) -> OrchestrationDetail:
    async with request.app.state.db.session_factory() as session:
        row = await session.get(Orchestration, orchestration_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Orchestration not found")
        attempts = (
            await session.scalars(
                select(AttemptLog)
                .where(AttemptLog.orchestration_id == orchestration_id)
                .order_by(AttemptLog.attempt_number)
            )
        ).all()
        audit = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.orchestration_id == orchestration_id)
                .order_by(AuditEvent.id)
            )
        ).all()

    return OrchestrationDetail(
        **OrchestrationOut.from_row(row).model_dump(),
        attempt_log=[AttemptOut.from_row(a) for a in attempts],
        audit_trail=[AuditOut.from_row(e) for e in audit],
    )


@v1.get("/circuit", summary="Current circuit breaker state")
async def circuit_state(request: Request, _principal: str = Depends(get_principal)) -> dict:
    return request.app.state.breaker.snapshot()


# ----------------------------------------------------------------- operations
@ops.get("/health/live", summary="Liveness probe")
async def live() -> dict:
    return {"status": "ok"}


@ops.get("/health/ready", summary="Readiness probe (checks the database)")
async def ready(request: Request) -> JSONResponse:
    ok = await request.app.state.db.ping()
    return JSONResponse(
        {"status": "ready" if ok else "unavailable", "database": "up" if ok else "down"},
        status_code=200 if ok else 503,
    )


@ops.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
