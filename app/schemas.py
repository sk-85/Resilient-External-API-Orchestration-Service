"""Request/response models (the public API contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import AttemptLog, AuditEvent, Orchestration

Status = Literal["running", "succeeded", "failed", "circuit_open", "error"]

# Only characters that are legal in a URL path. No scheme, host, query or fragment.
_PATH_PATTERN = r"^/[A-Za-z0-9\-._~%:@!$&'()*+,;=/]*$"


class OrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        pattern=_PATH_PATTERN,
        description="Path on the configured upstream, e.g. /inventory/42. The host is fixed by config.",
        examples=["/inventory/42"],
    )
    params: dict[str, str | int | float | bool] | None = Field(
        default=None, description="Query-string parameters."
    )
    payload: dict[str, Any] | list[Any] | None = Field(default=None, description="JSON request body.")
    allow_unsafe_retries: bool = Field(
        default=False,
        description=(
            "POST and PATCH are not idempotent, so by default they are attempted once. "
            "Set true only if the upstream de-duplicates (e.g. via an idempotency key)."
        ),
    )

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        if value.startswith("//"):
            raise ValueError("path must not start with '//'")
        lowered = value.lower()
        if "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
            raise ValueError("encoded dots and slashes are not allowed in path")
        if any(segment in (".", "..") for segment in value.split("/")):
            raise ValueError("path must not contain '.' or '..' segments")
        return value


class OrchestrationOut(BaseModel):
    id: str
    correlation_id: str
    status: Status
    method: str
    path: str
    upstream_status: int | None = None
    attempts: int
    duration_ms: float | None = None
    data: Any | None = Field(default=None, description="Upstream response body on success.")
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Orchestration) -> OrchestrationOut:
        return cls(
            id=row.id,
            correlation_id=row.correlation_id,
            status=row.status,  # type: ignore[arg-type]
            method=row.method,
            path=row.path,
            upstream_status=row.upstream_status,
            attempts=row.attempts,
            duration_ms=row.duration_ms,
            data=row.response_body,
            error=row.error,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


class AttemptOut(BaseModel):
    attempt_number: int
    started_at: datetime
    duration_ms: float
    success: bool
    status_code: int | None = None
    retryable: bool | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: AttemptLog) -> AttemptOut:
        return cls(
            attempt_number=row.attempt_number,
            started_at=row.started_at,
            duration_ms=row.duration_ms,
            success=row.success,
            status_code=row.status_code,
            retryable=row.retryable,
            error=row.error,
        )


class AuditOut(BaseModel):
    event: str
    actor: str
    detail: Any | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: AuditEvent) -> AuditOut:
        return cls(event=row.event, actor=row.actor, detail=row.detail, created_at=row.created_at)


class OrchestrationDetail(OrchestrationOut):
    attempt_log: list[AttemptOut]
    audit_trail: list[AuditOut]
