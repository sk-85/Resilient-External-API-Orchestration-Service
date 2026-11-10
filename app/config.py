"""Environment-driven configuration.

Every setting can be overridden with an ``ORCH_`` prefixed environment variable
(or a ``.env`` file), e.g. ``ORCH_RETRY_MAX_ATTEMPTS=6``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCH_", env_file=".env", extra="ignore")

    # --- general -----------------------------------------------------------
    app_name: str = "resilient-orchestrator"
    environment: str = "local"
    log_level: str = "INFO"

    # --- database ----------------------------------------------------------
    # SQLite keeps local runs zero-setup. docker-compose switches this to Postgres:
    #   postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str = "sqlite+aiosqlite:///./orchestrator.db"

    # --- external API ------------------------------------------------------
    # Callers only supply a *path*; the host is fixed here. This prevents the
    # service being used as an open proxy (SSRF).
    external_api_base_url: str = "http://localhost:9000"
    request_timeout_seconds: float = Field(3.0, gt=0)  # hard limit per attempt
    total_timeout_seconds: float = Field(15.0, gt=0)  # budget for all attempts + backoff
    max_response_bytes: int = Field(1_000_000, gt=0)

    # --- retry -------------------------------------------------------------
    retry_max_attempts: int = Field(4, ge=1, le=10)
    retry_base_delay_seconds: float = Field(0.2, ge=0)
    retry_max_delay_seconds: float = Field(5.0, ge=0)
    retry_jitter: bool = True

    # --- circuit breaker ---------------------------------------------------
    breaker_failure_threshold: int = Field(5, ge=1)
    breaker_recovery_seconds: float = Field(15.0, gt=0)
    breaker_half_open_max_calls: int = Field(1, ge=1)

    # --- auth --------------------------------------------------------------
    auth_mode: Literal["none", "api_key", "jwt"] = "none"
    api_keys: str = ""  # comma separated
    jwt_secret: str = ""
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"

    # --- tracing -----------------------------------------------------------
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""  # e.g. http://localhost:4318
    otel_console_exporter: bool = False  # print spans to stdout (handy locally)

    @model_validator(mode="after")
    def _check_auth_config(self) -> Settings:
        # Fail closed: never start with auth enabled but no credentials configured.
        if self.auth_mode == "api_key" and not self.api_key_list:
            raise ValueError("ORCH_AUTH_MODE=api_key requires ORCH_API_KEYS")
        if self.auth_mode == "jwt" and not self.jwt_secret:
            raise ValueError("ORCH_AUTH_MODE=jwt requires ORCH_JWT_SECRET")
        return self

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def upstream_base_url(self) -> str:
        return self.external_api_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
