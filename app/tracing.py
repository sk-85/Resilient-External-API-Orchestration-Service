"""Optional OpenTelemetry setup.

Tracing is off unless ``ORCH_OTEL_ENABLED=true``. Application code always uses the
OpenTelemetry *API* (a no-op when no SDK is configured), so nothing else changes.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import Settings

logger = logging.getLogger(__name__)


def setup_tracing(app: FastAPI, settings: Settings) -> None:
    if not settings.otel_enabled:
        return

    # Imported lazily so the SDK/instrumentation packages are only needed when enabled.
    from opentelemetry import trace
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.app_name, "deployment.environment": settings.environment}
        )
    )

    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if settings.otel_console_exporter or not settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health/.*,metrics")
    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry tracing enabled")
