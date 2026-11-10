# Resilient External API Orchestration Service

A FastAPI microservice that orchestrates calls to an external API with retries, exponential backoff, a circuit breaker, database-backed state, and built-in observability (structured logs, Prometheus metrics, OpenTelemetry traces).

Built to demonstrate reliability engineering, observable service design, and a clean, testable microservice architecture.

**Author:** Sonukumar

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
[![CI](https://github.com/sk-85/resilient-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/sk-85/resilient-orchestrator/actions/workflows/ci.yml)

---

## Overview

External APIs fail in many ways: timeouts, rate limits, partial responses, complete outages. When core systems call them directly, those failures spread.

This service sits between your internal systems and an external provider and gives them a stable interface:

- **isolates** external instability behind one consistent request/response contract
- **retries** transient failures with exponential backoff and jitter
- **stops calling** a failing upstream with a circuit breaker, so failures don't cascade
- **records** every attempt and outcome in a database (state, error log, audit trail)
- **exposes** logs, metrics and traces so you can see what happened and why

It is the kind of internal tool SRE and platform teams build to keep distributed systems stable.

## How a request flows

```mermaid
flowchart LR
    C[Client] -->|POST /v1/orchestrations| A[API layer<br/>validation + auth]
    A --> O[Orchestrator]
    O -->|1. persist 'running'| DB[(PostgreSQL / SQLite)]
    O --> R[Retry engine<br/>backoff + jitter + deadline]
    R --> B{Circuit breaker}
    B -- closed / probe --> X[External client<br/>timeout + classification]
    B -- open --> F[Fail fast: 503 + Retry-After]
    X --> U[(External API)]
    O -->|2. persist outcome,<br/>attempts, audit trail| DB
```

1. The request is validated (path-only, no host), authenticated, and stamped with a correlation ID.
2. The orchestration is **saved as `running` first**, so it is auditable even if the process dies mid-call.
3. Each attempt passes through the **circuit breaker**, then the **external client**, which enforces a hard per-attempt timeout and classifies the outcome as retryable or not.
4. The **retry engine** waits (exponential backoff + full jitter, honouring `Retry-After`) and tries again until it succeeds, hits a non-retryable error, runs out of attempts, or runs out of time budget.
5. The final status, every attempt, and an audit event are written to the database, and a consistent JSON response is returned.

## Resilience patterns

| Pattern | What it does here | Benefit |
|---|---|---|
| Retry logic | Re-attempts timeouts, connection errors, HTTP 5xx, 408 and 429 | Absorbs transient failures |
| Exponential backoff + full jitter | Wait grows `base × 2^n`, capped, randomised in `[0, ceiling]` | Avoids hammering a struggling API; avoids lockstep retries |
| `Retry-After` support | Waits at least as long as the upstream asks on 429/503 | Cooperates with rate limiting |
| Circuit breaker | Opens after N consecutive failures, probes after a cooldown (closed → open → half-open) | Prevents cascading failures and wasted calls |
| Timeout enforcement | Hard deadline per attempt **and** a total time budget per request | Keeps the service responsive when upstreams hang |
| Retry safety | `POST`/`PATCH` are attempted once unless the caller opts in | Prevents duplicated side effects |
| Structured logging | JSON logs with correlation ID (and trace ID when tracing is on) | Easy debugging and log search |
| Distributed tracing | OpenTelemetry spans for the request, the orchestration and the outbound HTTP call | End-to-end visibility |
| Metrics | Latency, error rates, retries, breaker state at `/metrics` | Dashboards and alerts |

## Quick start (local, no Docker)

Requires Python 3.11+.

```bash
git clone https://github.com/sk-85/resilient-orchestrator.git
cd resilient-orchestrator

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest            # run the test suite first
```

Then run the service against the bundled flaky mock upstream, in three terminals:

```bash
# terminal 1: a deliberately unreliable "external API" on :9000
python mock_external/server.py

# terminal 2: the service on :8000 (uses SQLite by default)
uvicorn app.main:create_app --factory --reload

# terminal 3: walk through the resilience behaviour
bash scripts/demo.sh
```

Interactive API docs are at <http://localhost:8000/docs>.

### With Docker (service + PostgreSQL + mock upstream)

```bash
docker compose up --build
bash scripts/demo.sh
```

## Try it yourself

```bash
# Upstream fails twice then recovers: the service retries and succeeds on attempt 3
curl -s -X POST localhost:8000/v1/orchestrations \
  -H 'Content-Type: application/json' \
  -d '{"path": "/fail-then-succeed", "params": {"fails": 2, "key": "try1"}}'
```

```json
{
  "id": "8d3c1f0e-…",
  "correlation_id": "5f0b2a…",
  "status": "succeeded",
  "method": "GET",
  "path": "/fail-then-succeed",
  "upstream_status": 200,
  "attempts": 3,
  "duration_ms": 412.7,
  "data": { "message": "recovered", "call": 3 },
  "error": null,
  "created_at": "2026-11-09T23:50:01.120Z",
  "completed_at": "2026-11-09T23:50:01.533Z"
}
```

Break the upstream and watch the circuit open:

```bash
curl -s localhost:9000/admin/outage/on
for i in 1 2 3; do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/orchestrations \
    -H 'Content-Type: application/json' -d '{"path": "/ok"}'
done                                   # 502, 503, 503 ...
curl -s localhost:8000/v1/circuit      # {"state": "open", ...}
curl -s localhost:9000/admin/outage/off
# after ORCH_BREAKER_RECOVERY_SECONDS the breaker probes once and closes again
```

Then inspect the full history of any run, including every attempt:

```bash
curl -s localhost:8000/v1/orchestrations/<id> | python3 -m json.tool
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/orchestrations` | Call the upstream with retries and circuit breaking |
| `GET` | `/v1/orchestrations` | List recent orchestrations (`?status=`, `?limit=`, `?offset=`) |
| `GET` | `/v1/orchestrations/{id}` | One orchestration with its attempt log and audit trail |
| `GET` | `/v1/circuit` | Current circuit breaker state |
| `GET` | `/health/live` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (checks the database) |
| `GET` | `/metrics` | Prometheus metrics |

**Request body** for `POST /v1/orchestrations`:

| Field | Type | Notes |
|---|---|---|
| `path` | string, required | Path on the configured upstream, e.g. `/inventory/42`. No scheme, host or query string. |
| `method` | `GET` (default), `POST`, `PUT`, `PATCH`, `DELETE` | |
| `params` | object | Query-string parameters |
| `payload` | object or array | JSON request body |
| `allow_unsafe_retries` | bool, default `false` | Allow retrying `POST`/`PATCH`. Only enable if the upstream de-duplicates requests. |

**Response status codes** (the JSON body always has the same shape):

| Final status | HTTP | Meaning |
|---|---|---|
| `succeeded` | 200 | Upstream answered successfully |
| `failed` | 502 | Upstream failed after all retries, or rejected the request with a non-retryable 4xx |
| `circuit_open` | 503 | Circuit is open; the upstream was not called. `Retry-After` says when to try again |
| `error` | 500 | Unexpected internal error |

Every response carries an `X-Request-ID` header. Send your own to correlate across systems; it is also forwarded to the upstream.

## Configuration

All settings are environment variables prefixed `ORCH_` (or in a `.env` file; see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_DATABASE_URL` | `sqlite+aiosqlite:///./orchestrator.db` | Use `postgresql+asyncpg://user:pass@host:5432/db` for PostgreSQL |
| `ORCH_EXTERNAL_API_BASE_URL` | `http://localhost:9000` | The one upstream this service may call |
| `ORCH_REQUEST_TIMEOUT_SECONDS` | `3` | Hard limit per attempt |
| `ORCH_TOTAL_TIMEOUT_SECONDS` | `15` | Time budget for all attempts plus backoff |
| `ORCH_RETRY_MAX_ATTEMPTS` | `4` | Attempts including the first |
| `ORCH_RETRY_BASE_DELAY_SECONDS` | `0.2` | First backoff step |
| `ORCH_RETRY_MAX_DELAY_SECONDS` | `5` | Backoff cap |
| `ORCH_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures before the circuit opens |
| `ORCH_BREAKER_RECOVERY_SECONDS` | `15` | Cooldown before a half-open probe |
| `ORCH_AUTH_MODE` | `none` | `none`, `api_key` (`X-API-Key` header) or `jwt` (Bearer, HS256) |
| `ORCH_API_KEYS` | | Comma-separated keys for `api_key` mode |
| `ORCH_JWT_SECRET` | | Shared secret for `jwt` mode (tokens need `sub` and `exp`) |
| `ORCH_OTEL_ENABLED` | `false` | Turn on OpenTelemetry tracing |
| `ORCH_OTEL_EXPORTER_OTLP_ENDPOINT` | | e.g. `http://localhost:4318` |

The service refuses to start if auth is enabled without credentials configured.

## Observability

- **Logs**: one JSON line per request and per orchestration, each with `correlation_id` (and `trace_id` / `span_id` when tracing is on).
- **Metrics** (`/metrics`): `orchestrator_http_requests_total`, `orchestrator_http_request_duration_seconds`, `orchestrator_orchestrations_total{status}`, `orchestrator_external_calls_total{outcome}`, `orchestrator_external_call_duration_seconds`, `orchestrator_retries_total`, `orchestrator_circuit_breaker_state{upstream}` (0 closed, 1 half-open, 2 open) and `orchestrator_circuit_breaker_transitions_total`.
- **Traces**: set `ORCH_OTEL_ENABLED=true` (add `ORCH_OTEL_CONSOLE_EXPORTER=true` to print spans locally, or point `ORCH_OTEL_EXPORTER_OTLP_ENDPOINT` at a collector).

## Project layout

```
app/
  main.py                 app factory, lifespan wiring, observability middleware
  routes.py               HTTP endpoints
  schemas.py              request/response models and input validation
  models.py, database.py  SQLAlchemy async models and engine
  auth.py                 API-key / JWT authentication
  config.py               environment-driven settings
  logging_config.py       structured JSON logging
  metrics.py, tracing.py  Prometheus and OpenTelemetry
  services/
    retry.py              retry + backoff + jitter + deadline
    circuit_breaker.py    closed / open / half-open state machine
    external_client.py    httpx wrapper that classifies failures
    orchestrator.py       persist → retry/breaker → persist flow
mock_external/server.py   flaky mock upstream for local testing (stdlib only)
scripts/demo.sh           guided walkthrough of the resilience behaviour
tests/                    unit tests and end-to-end API tests
```

## Testing

```bash
pytest                                  # everything
pytest --cov=app --cov-report=term-missing
ruff check .                            # lint
```

- `test_retry.py`, `test_circuit_breaker.py`: pure unit tests with a virtual clock, so they run instantly and are deterministic.
- `test_external_client.py`: failure classification (which statuses and errors are retryable), timeouts, `Retry-After` parsing.
- `test_api.py`: end-to-end through the real FastAPI app and a real SQLite database, with the upstream replaced by a scripted fake. Covers retries, exhaustion, circuit opening and fail-fast, non-retry of 4xx and `POST`, persistence of attempts and audit trail, input validation, correlation IDs, auth, health and metrics.

CI runs lint, tests (Python 3.11 and 3.12) and a Docker build on every push and pull request.

## Design decisions

- **Path-only requests.** Callers supply a path; the host comes from configuration. This stops the service being used as an open proxy (SSRF). Redirects are never followed, and dot-segments and encoded slashes are rejected.
- **Retries respect HTTP semantics.** Only timeouts, connection errors, 5xx, 408 and 429 are retried. A 404 is a definitive answer, so it is not retried and does not count against the circuit breaker.
- **Non-idempotent methods are attempted once** unless the caller opts in with `allow_unsafe_retries`.
- **The breaker counts attempts, not requests.** A request that retries three times contributes three failures, so a persistently broken upstream trips the breaker quickly.
- **Persist before calling.** The `running` record exists before the first upstream call, so crashes leave evidence.
- **Two time limits.** A hard per-attempt timeout stops a slow upstream holding a request, and a total budget stops backoff from stretching a request indefinitely.
- **Injectable clock, sleep and RNG** in the retry and breaker code, which is what makes them testable without real waiting.

## Known limitations

This is a reference implementation. Before running it for real, consider:

- **Circuit breaker state is per process.** With several replicas, each tracks the upstream independently. Sharing state (for example via Redis) is a natural next step.
- **Schema is created at startup** with `create_all`. Use Alembic migrations for real deployments.
- **A request holds its connection while retries run** (up to `ORCH_TOTAL_TIMEOUT_SECONDS`). For long-running work, an asynchronous job queue would suit better.
- **Single upstream.** Supporting several would mean one breaker per upstream.
- **No per-caller data isolation:** any authenticated caller can read any orchestration. `/metrics` and the health endpoints are unauthenticated, so restrict them at the network layer.
- **Idempotency keys** for caller-side de-duplication are not implemented.

## Roadmap ideas

Alembic migrations · shared breaker state · idempotency keys · per-upstream configuration · rate limiting · a Grafana dashboard for the exported metrics.

## License

[MIT](LICENSE) © 2026 Sonukumar
