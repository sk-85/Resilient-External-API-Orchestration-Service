"""SQLAlchemy models: orchestration state, per-attempt log, and audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class Orchestration(Base):
    """One orchestrated call: its request, final outcome and upstream response."""

    __tablename__ = "orchestrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(2048))
    # running | succeeded | failed | circuit_open | error
    status: Mapped[str] = mapped_column(String(16), index=True)
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    response_body: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AttemptLog(Base):
    """Every attempt against the upstream, including failures (the error log)."""

    __tablename__ = "attempt_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orchestration_id: Mapped[str] = mapped_column(ForeignKey("orchestrations.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    duration_ms: Mapped[float] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEvent(Base):
    """Append-only trail of who did what, and when."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orchestration_id: Mapped[str] = mapped_column(ForeignKey("orchestrations.id"), index=True)
    event: Mapped[str] = mapped_column(String(32))  # created | succeeded | failed | ...
    actor: Mapped[str] = mapped_column(String(128))
    detail: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
