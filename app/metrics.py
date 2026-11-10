"""Prometheus metrics, exposed at ``/metrics``."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "orchestrator_http_requests_total",
    "Inbound HTTP requests.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "orchestrator_http_request_duration_seconds",
    "Inbound HTTP request latency.",
    ["method", "route"],
)

ORCHESTRATIONS = Counter(
    "orchestrator_orchestrations_total",
    "Finished orchestrations by final status.",
    ["status"],
)
EXTERNAL_CALLS = Counter(
    "orchestrator_external_calls_total",
    "Individual upstream attempts by outcome "
    "(success, retryable_error, client_error, circuit_open).",
    ["outcome"],
)
EXTERNAL_LATENCY = Histogram(
    "orchestrator_external_call_duration_seconds",
    "Latency of individual upstream attempts.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
RETRIES = Counter(
    "orchestrator_retries_total",
    "Retries scheduled after a failed attempt.",
)

CIRCUIT_STATE = Gauge(
    "orchestrator_circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=half_open, 2=open.",
    ["upstream"],
)
CIRCUIT_TRANSITIONS = Counter(
    "orchestrator_circuit_breaker_transitions_total",
    "Circuit breaker state transitions.",
    ["upstream", "to_state"],
)

CIRCUIT_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}
