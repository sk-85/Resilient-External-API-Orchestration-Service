import asyncio

import pytest

httpx = pytest.importorskip("httpx")

from app.services.external_client import ExternalAPIError, ExternalClient  # noqa: E402

pytestmark = pytest.mark.anyio


def make_client(handler, *, timeout: float = 1.0, max_bytes: int = 1_000_000) -> ExternalClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExternalClient(http, "http://upstream.test/", timeout, max_bytes)


async def test_success_parses_json_body():
    client = make_client(lambda req: httpx.Response(200, json={"hello": "world"}))
    result = await client.request("GET", "/ok")
    assert result.status_code == 200
    assert result.body == {"hello": "world"}


async def test_builds_url_from_base_and_path_and_forwards_params_and_headers():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["rid"] = req.headers.get("x-request-id")
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.request("GET", "/items", params={"q": "a"}, headers={"X-Request-ID": "abc"})
    assert seen == {"url": "http://upstream.test/items?q=a", "rid": "abc"}


async def test_plain_text_body_is_returned_as_text():
    client = make_client(lambda req: httpx.Response(200, text="pong"))
    assert (await client.request("GET", "/ping")).body == "pong"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 429])
async def test_retryable_statuses(status):
    client = make_client(lambda req: httpx.Response(status))
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retryable is True
    assert info.value.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retryable(status):
    client = make_client(lambda req: httpx.Response(status))
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retryable is False
    assert info.value.status_code == status


async def test_redirects_are_not_followed_or_retried():
    client = make_client(lambda req: httpx.Response(302, headers={"location": "http://evil.test"}))
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retryable is False


async def test_retry_after_header_is_parsed():
    client = make_client(lambda req: httpx.Response(429, headers={"retry-after": "7"}))
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retry_after == 7.0


@pytest.mark.parametrize("value", ["soon", "-3", "nan", "inf"])
async def test_garbage_retry_after_is_ignored(value):
    client = make_client(lambda req: httpx.Response(503, headers={"retry-after": value}))
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retry_after is None


async def test_connection_errors_are_retryable():
    def handler(req):
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retryable is True
    assert info.value.status_code is None


async def test_httpx_timeouts_are_retryable():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    client = make_client(handler)
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/x")
    assert info.value.retryable is True


async def test_hard_per_attempt_deadline_is_enforced():
    async def handler(req):
        await asyncio.sleep(1)
        return httpx.Response(200)

    client = make_client(handler, timeout=0.05)
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/slow")
    assert info.value.retryable is True
    assert "within" in str(info.value)


async def test_oversized_response_is_rejected_without_retry():
    client = make_client(lambda req: httpx.Response(200, content=b"x" * 100), max_bytes=10)
    with pytest.raises(ExternalAPIError) as info:
        await client.request("GET", "/big")
    assert info.value.retryable is False
