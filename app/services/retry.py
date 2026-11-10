"""Retry with exponential backoff and full jitter.

Dependency-free on purpose: the clock, sleep and RNG are injectable so the
behaviour is fully testable without real waiting.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.2
    max_delay: float = 5.0
    multiplier: float = 2.0
    jitter: bool = True

    def backoff(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Delay before the retry that follows failed attempt number ``attempt`` (1-based).

        Exponential growth capped at ``max_delay``. With jitter enabled the actual
        delay is uniform in [0, ceiling] ("full jitter"), which spreads out clients
        that failed at the same moment so they don't retry in lockstep.
        """
        ceiling = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        return rng() * ceiling if self.jitter else ceiling


@dataclass
class Attempt(Generic[T]):
    """Record of one attempt, handed to ``on_attempt`` so callers can audit it."""

    number: int
    started_at: datetime
    duration_ms: float
    result: T | None = None
    error: Exception | None = None

    @property
    def success(self) -> bool:
        return self.error is None


async def retry_async(
    func: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[Exception], bool],
    on_attempt: Callable[[Attempt[T]], None] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    deadline: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call ``func(attempt_number)`` until it succeeds, is not retryable, or gives up.

    Gives up (re-raising the last error) when any of these is true:
      * ``policy.max_attempts`` attempts have been made,
      * ``is_retryable(error)`` is False,
      * waiting for the next retry would pass ``deadline`` (a ``clock()`` timestamp).

    If the error carries a ``retry_after`` attribute (seconds), the wait is at least
    that long, so an upstream's ``Retry-After`` hint is honoured.
    """
    attempt_no = 0
    while True:
        attempt_no += 1
        started_at = datetime.now(UTC)
        t0 = clock()
        try:
            result = await func(attempt_no)
        except Exception as exc:
            duration_ms = (clock() - t0) * 1000
            if on_attempt:
                on_attempt(Attempt(attempt_no, started_at, duration_ms, error=exc))

            if attempt_no >= policy.max_attempts or not is_retryable(exc):
                raise

            delay = policy.backoff(attempt_no, rng)
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                delay = max(delay, retry_after)

            if deadline is not None and clock() + delay >= deadline:
                raise  # not enough time budget left to wait and try again

            if on_retry:
                on_retry(attempt_no, delay, exc)
            await sleep(delay)
        else:
            duration_ms = (clock() - t0) * 1000
            if on_attempt:
                on_attempt(Attempt(attempt_no, started_at, duration_ms, result=result))
            return result
