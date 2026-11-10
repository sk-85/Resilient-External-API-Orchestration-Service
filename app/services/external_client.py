"""Thin wrapper around httpx that classifies every failure as retryable or not."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

import httpx


class ExternalAPIError(Exception):
    """A failed upstream call. ``retryable`` tells the retry engine whether to try again."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class ExternalResponse:
    status_code: int
    body: Any


def _parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After`` given in seconds. HTTP-date form is ignored."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    if "json" in response.headers.get("content-type", "").lower():
        try:
            return response.json()
        except ValueError:
            pass
    return response.text[:10_000]


class ExternalClient:
    """Calls ``base_url + path`` and turns outcomes into responses or ``ExternalAPIError``.

    Classification:
      * timeouts, connection errors, HTTP 5xx, 408, 429  -> retryable
      * other 4xx, 3xx redirects, oversized responses     -> not retryable
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        timeout: float,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> ExternalResponse:
        url = f"{self._base_url}{path}"
        try:
            # Hard per-attempt deadline. httpx timeouts are per phase, so a server
            # that trickles bytes could otherwise hold a request open for much longer.
            async with asyncio.timeout(self._timeout):
                response = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
        except TimeoutError as exc:
            raise ExternalAPIError(
                f"upstream did not answer within {self._timeout}s", retryable=True
            ) from exc
        except httpx.TimeoutException as exc:
            raise ExternalAPIError(f"upstream timed out ({type(exc).__name__})", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ExternalAPIError(
                f"could not reach upstream ({type(exc).__name__}: {exc})", retryable=True
            ) from exc

        status = response.status_code

        if len(response.content) > self._max_response_bytes:
            raise ExternalAPIError(
                f"upstream response exceeds {self._max_response_bytes} bytes", status_code=status
            )
        if status >= 500:
            raise ExternalAPIError(
                f"upstream returned HTTP {status}",
                status_code=status,
                retryable=True,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        if status in (408, 429):
            raise ExternalAPIError(
                f"upstream returned HTTP {status}",
                status_code=status,
                retryable=True,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        if status >= 400:
            raise ExternalAPIError(f"upstream returned HTTP {status}", status_code=status)
        if status >= 300:
            raise ExternalAPIError(f"unexpected redirect (HTTP {status})", status_code=status)

        return ExternalResponse(status_code=status, body=_parse_body(response))
