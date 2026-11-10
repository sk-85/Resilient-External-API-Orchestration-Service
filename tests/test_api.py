"""End-to-end API tests.

The upstream is replaced by ``httpx.MockTransport``, so no network or mock server
is needed. A real SQLite database (in a temp dir) backs each test.
"""

import time
from contextlib import AsyncExitStack

import pytest

for _module in ("httpx", "fastapi", "sqlalchemy", "aiosqlite", "prometheus_client", "pydantic_settings"):
    pytest.importorskip(_module)

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

pytestmark = pytest.mark.anyio


class ScriptedUpstream:
    """Fake upstream. Each step is (status, json_body, headers) or an Exception to raise.
    The last step repeats forever."""

    def __init__(self, *steps):
        self.steps = steps
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        step = self.steps[min(len(self.calls) - 1, len(self.steps) - 1)]
        if isinstance(step, Exception):
            raise step
        status, body, headers = step
        return httpx.Response(status, json=body, headers=headers)


def ok(body=None):
    return (200, body if body is not None else {"ok": True}, {})


def fail(status=503):
    return (status, {"error": "nope"}, {})


@pytest.fixture
async def make_service(tmp_path):
    """Factory: start an app wired to a scripted upstream; returns (client, upstream)."""
    stack = AsyncExitStack()

    async def _make(upstream: ScriptedUpstream, **overrides):
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            external_api_base_url="http://upstream.test",
            retry_base_delay_seconds=0.001,  # keep retries fast
            retry_max_delay_seconds=0.005,
            **overrides,
        )
        upstream_http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_app(settings, http_client=upstream_http)
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        )
        return client

    yield _make
    await stack.aclose()


async def post(client, path="/items/1", **body):
    return await client.post("/v1/orchestrations", json={"path": path, **body})


# ------------------------------------------------------------------ happy path
async def test_success_on_first_attempt(make_service):
    upstream = ScriptedUpstream(ok({"id": 1}))
    client = await make_service(upstream)

    r = await post(client)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["attempts"] == 1
    assert body["upstream_status"] == 200
    assert body["data"] == {"id": 1}
    assert len(upstream.calls) == 1
    assert str(upstream.calls[0].url) == "http://upstream.test/items/1"


async def test_transient_failures_are_retried_until_success(make_service):
    upstream = ScriptedUpstream(fail(503), fail(500), ok({"id": 1}))
    client = await make_service(upstream)

    r = await post(client)

    assert r.status_code == 200
    assert r.json()["attempts"] == 3
    assert len(upstream.calls) == 3


async def test_connection_errors_and_timeouts_are_retried(make_service):
    upstream = ScriptedUpstream(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok())
    client = await make_service(upstream)

    r = await post(client)

    assert r.status_code == 200
    assert r.json()["attempts"] == 3


async def test_429_with_retry_after_is_retried(make_service):
    upstream = ScriptedUpstream((429, {}, {"retry-after": "0"}), ok())
    client = await make_service(upstream)

    r = await post(client)

    assert r.status_code == 200
    assert r.json()["attempts"] == 2


# ------------------------------------------------------------------- failures
async def test_gives_up_after_max_attempts_with_502(make_service):
    upstream = ScriptedUpstream(fail(500))
    client = await make_service(upstream, retry_max_attempts=3, breaker_failure_threshold=100)

    r = await post(client)

    assert r.status_code == 502
    body = r.json()
    assert body["status"] == "failed"
    assert body["attempts"] == 3
    assert body["upstream_status"] == 500
    assert "HTTP 500" in body["error"]
    assert len(upstream.calls) == 3


async def test_client_errors_are_not_retried(make_service):
    upstream = ScriptedUpstream((404, {"error": "missing"}, {}))
    client = await make_service(upstream)

    r = await post(client)

    assert r.status_code == 502
    assert r.json()["attempts"] == 1
    assert r.json()["upstream_status"] == 404
    assert len(upstream.calls) == 1


async def test_post_is_not_retried_unless_explicitly_allowed(make_service):
    upstream = ScriptedUpstream(fail(503))
    client = await make_service(upstream, retry_max_attempts=3, breaker_failure_threshold=100)

    r = await post(client, method="POST", payload={"x": 1})
    assert r.json()["attempts"] == 1

    r = await post(client, method="POST", payload={"x": 1}, allow_unsafe_retries=True)
    assert r.json()["attempts"] == 3


# ---------------------------------------------------------------- circuit breaker
async def test_circuit_opens_then_fails_fast_without_calling_upstream(make_service):
    upstream = ScriptedUpstream(fail(500))
    client = await make_service(upstream, breaker_failure_threshold=3, retry_max_attempts=4)

    first = await post(client)
    # 3 real failures trip the breaker; the 4th attempt is refused locally.
    assert first.status_code == 503
    assert first.json()["status"] == "circuit_open"
    assert first.json()["attempts"] == 4
    assert first.headers["retry-after"] == "15"
    assert len(upstream.calls) == 3

    second = await post(client)
    assert second.status_code == 503
    assert second.json()["attempts"] == 1
    assert len(upstream.calls) == 3  # upstream was not touched

    circuit = (await client.get("/v1/circuit")).json()
    assert circuit["state"] == "open"


async def test_404s_do_not_trip_the_breaker(make_service):
    upstream = ScriptedUpstream((404, {}, {}))
    client = await make_service(upstream, breaker_failure_threshold=2)

    for _ in range(5):
        await post(client)

    assert (await client.get("/v1/circuit")).json()["state"] == "closed"
    assert len(upstream.calls) == 5


# ----------------------------------------------------------------- persistence
async def test_attempts_and_audit_trail_are_persisted(make_service):
    upstream = ScriptedUpstream(fail(503), fail(503), ok())
    client = await make_service(upstream)

    created = (await post(client)).json()
    detail = (await client.get(f"/v1/orchestrations/{created['id']}")).json()

    assert detail["status"] == "succeeded"
    assert [a["status_code"] for a in detail["attempt_log"]] == [503, 503, 200]
    assert [a["success"] for a in detail["attempt_log"]] == [False, False, True]
    assert "HTTP 503" in detail["attempt_log"][0]["error"]
    assert [e["event"] for e in detail["audit_trail"]] == ["created", "succeeded"]
    assert detail["audit_trail"][0]["actor"] == "anonymous"


async def test_list_orchestrations_with_status_filter(make_service):
    upstream = ScriptedUpstream(ok(), (404, {}, {}))
    client = await make_service(upstream)
    await post(client)
    await post(client)

    everything = (await client.get("/v1/orchestrations")).json()
    failed_only = (await client.get("/v1/orchestrations", params={"status": "failed"})).json()

    assert len(everything) == 2
    assert [r["status"] for r in failed_only] == ["failed"]


async def test_unknown_orchestration_returns_404(make_service):
    client = await make_service(ScriptedUpstream(ok()))
    r = await client.get("/v1/orchestrations/does-not-exist")
    assert r.status_code == 404


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "bad_body",
    [
        {"path": "//evil.example/x"},
        {"path": "http://evil.example/x"},
        {"path": "no-leading-slash"},
        {"path": "/a/../etc/passwd"},
        {"path": "/a/%2e%2e/b"},
        {"path": "/ok", "method": "TRACE"},
        {"path": "/ok", "surprise": True},
    ],
)
async def test_invalid_requests_are_rejected_before_any_upstream_call(make_service, bad_body):
    upstream = ScriptedUpstream(ok())
    client = await make_service(upstream)

    r = await client.post("/v1/orchestrations", json=bad_body)

    assert r.status_code == 422
    assert upstream.calls == []


# ---------------------------------------------------------------- observability
async def test_correlation_id_is_echoed_and_forwarded_upstream(make_service):
    upstream = ScriptedUpstream(ok())
    client = await make_service(upstream)

    r = await client.post(
        "/v1/orchestrations", json={"path": "/x"}, headers={"X-Request-ID": "abc-123"}
    )

    assert r.headers["x-request-id"] == "abc-123"
    assert r.json()["correlation_id"] == "abc-123"
    assert upstream.calls[0].headers["x-request-id"] == "abc-123"


async def test_malformed_correlation_id_is_replaced(make_service):
    client = await make_service(ScriptedUpstream(ok()))
    r = await client.get("/health/live", headers={"X-Request-ID": "bad id with spaces!"})
    assert r.headers["x-request-id"] != "bad id with spaces!"
    assert len(r.headers["x-request-id"]) == 32


async def test_health_and_metrics_endpoints(make_service):
    client = await make_service(ScriptedUpstream(ok()))
    await post(client)

    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "up"

    metrics = (await client.get("/metrics")).text
    assert "orchestrator_orchestrations_total" in metrics
    assert "orchestrator_circuit_breaker_state" in metrics
    assert 'route="/v1/orchestrations"' in metrics


# ------------------------------------------------------------------------ auth
async def test_api_key_auth(make_service):
    client = await make_service(ScriptedUpstream(ok()), auth_mode="api_key", api_keys="key-one,key-two")

    assert (await post(client)).status_code == 401  # no key

    wrong = await client.post(
        "/v1/orchestrations", json={"path": "/x"}, headers={"X-API-Key": "wrong"}
    )
    assert wrong.status_code == 401

    good = await client.post(
        "/v1/orchestrations", json={"path": "/x"}, headers={"X-API-Key": "key-two"}
    )
    assert good.status_code == 200
    assert (await client.get("/health/live")).status_code == 200  # probes stay open

    detail = (
        await client.get(f"/v1/orchestrations/{good.json()['id']}", headers={"X-API-Key": "key-two"})
    ).json()
    assert detail["audit_trail"][0]["actor"].startswith("api-key:")
    assert "key-two" not in str(detail)  # the key itself is never stored


async def test_jwt_auth(make_service):
    jwt = pytest.importorskip("jwt")
    secret = "x" * 32
    client = await make_service(ScriptedUpstream(ok()), auth_mode="jwt", jwt_secret=secret)

    assert (await post(client)).status_code == 401

    expired = jwt.encode({"sub": "alice", "exp": int(time.time()) - 10}, secret, algorithm="HS256")
    r = await client.post(
        "/v1/orchestrations", json={"path": "/x"}, headers={"Authorization": f"Bearer {expired}"}
    )
    assert r.status_code == 401

    token = jwt.encode({"sub": "alice", "exp": int(time.time()) + 60}, secret, algorithm="HS256")
    r = await client.post(
        "/v1/orchestrations", json={"path": "/x"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    detail = (
        await client.get(
            f"/v1/orchestrations/{r.json()['id']}", headers={"Authorization": f"Bearer {token}"}
        )
    ).json()
    assert detail["audit_trail"][0]["actor"] == "alice"


def test_auth_misconfiguration_fails_at_startup():
    with pytest.raises(ValueError):
        Settings(_env_file=None, auth_mode="api_key", api_keys="")
    with pytest.raises(ValueError):
        Settings(_env_file=None, auth_mode="jwt", jwt_secret="")
