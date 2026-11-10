"""Circuit breaker (closed -> open -> half-open -> closed).

State is kept per process. All state changes happen synchronously (no ``await``
between check and update), so it is safe under asyncio without locks.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal operation, calls pass through
    OPEN = "open"  # failing fast, calls are rejected
    HALF_OPEN = "half_open"  # letting a limited number of probe calls through


class CircuitOpenError(Exception):
    """Raised instead of calling the upstream while the circuit is open."""

    def __init__(self, name: str, retry_in: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_in:.1f}s")
        self.name = name
        self.retry_in = retry_in


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 15.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._on_state_change = on_state_change

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> CircuitState:
        self._refresh()
        return self._state

    def _refresh(self) -> None:
        """Move OPEN -> HALF_OPEN once the recovery timeout has elapsed."""
        if self._state is CircuitState.OPEN and self._remaining() <= 0:
            self._set_state(CircuitState.HALF_OPEN)

    def _remaining(self) -> float:
        return max(0.0, self.recovery_timeout - (self._clock() - self._opened_at))

    def _set_state(self, new: CircuitState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        if new is CircuitState.OPEN:
            self._opened_at = self._clock()
        elif new is CircuitState.HALF_OPEN:
            self._half_open_in_flight = 0
        elif new is CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._half_open_in_flight = 0
        if self._on_state_change:
            self._on_state_change(self.name, old, new)

    # --------------------------------------------------------------- call flow
    def before_call(self) -> None:
        """Gate a call. Raises ``CircuitOpenError`` if it must not go through."""
        self._refresh()
        if self._state is CircuitState.OPEN:
            raise CircuitOpenError(self.name, self._remaining())
        if self._state is CircuitState.HALF_OPEN:
            if self._half_open_in_flight >= self.half_open_max_calls:
                # A probe is already in flight; everyone else waits for its verdict.
                raise CircuitOpenError(self.name, 1.0)
            self._half_open_in_flight += 1

    def record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._set_state(CircuitState.CLOSED)  # probe succeeded: upstream recovered
        elif self._state is CircuitState.CLOSED:
            self._consecutive_failures = 0
        # OPEN: a straggler that started before we opened; ignore it.

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._set_state(CircuitState.OPEN)  # probe failed: back off again
        elif self._state is CircuitState.CLOSED:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._set_state(CircuitState.OPEN)

    def release(self) -> None:
        """Give back a half-open probe slot without judging upstream health.

        Used when a call is cancelled or hits an unrelated bug, so the breaker
        cannot get stuck half-open with a probe that never reports back.
        """
        if self._state is CircuitState.HALF_OPEN and self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        is_failure: Callable[[Exception], bool] = lambda exc: True,
        **kwargs: Any,
    ) -> T:
        """Run ``func`` through the breaker.

        ``is_failure`` decides whether an exception means "the upstream is unhealthy"
        (counts toward opening) or not (e.g. a 404 shows the upstream is fine).
        """
        self.before_call()
        try:
            result = await func(*args, **kwargs)
        except Exception as exc:
            if is_failure(exc):
                self.record_failure()
            else:
                self.record_success()
            raise
        except BaseException:  # cancellation etc.
            self.release()
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        return {
            "name": self.name,
            "state": state.value,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_seconds": self.recovery_timeout,
            "retry_in_seconds": round(self._remaining(), 2) if state is CircuitState.OPEN else 0.0,
        }
