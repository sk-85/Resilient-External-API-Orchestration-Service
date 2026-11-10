import pytest

from app.services.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(clock: FakeClock, **kw) -> CircuitBreaker:
    return CircuitBreaker(
        "upstream", failure_threshold=3, recovery_timeout=10.0, clock=clock, **kw
    )


def trip(cb: CircuitBreaker) -> None:
    for _ in range(cb.failure_threshold):
        cb.before_call()
        cb.record_failure()


def test_starts_closed_and_allows_calls():
    cb = make(FakeClock())
    assert cb.state is CircuitState.CLOSED
    cb.before_call()  # does not raise


def test_opens_after_threshold_consecutive_failures():
    cb = make(FakeClock())
    for _ in range(2):
        cb.before_call()
        cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    cb.before_call()
    cb.record_failure()
    assert cb.state is CircuitState.OPEN


def test_a_success_resets_the_failure_count():
    cb = make(FakeClock())
    for _ in range(2):
        cb.before_call()
        cb.record_failure()
    cb.before_call()
    cb.record_success()
    for _ in range(2):  # two more failures: still below the threshold of 3
        cb.before_call()
        cb.record_failure()
    assert cb.state is CircuitState.CLOSED


def test_open_circuit_rejects_calls_and_reports_wait_time():
    clock = FakeClock()
    cb = make(clock)
    trip(cb)
    clock.advance(4)
    with pytest.raises(CircuitOpenError) as info:
        cb.before_call()
    assert info.value.retry_in == pytest.approx(6.0)


def test_moves_to_half_open_after_recovery_timeout():
    clock = FakeClock()
    cb = make(clock)
    trip(cb)
    clock.advance(10)
    assert cb.state is CircuitState.HALF_OPEN


def test_successful_probe_closes_the_circuit():
    clock = FakeClock()
    cb = make(clock)
    trip(cb)
    clock.advance(10)
    cb.before_call()  # the probe
    cb.record_success()
    assert cb.state is CircuitState.CLOSED
    cb.before_call()  # traffic flows again


def test_failed_probe_reopens_with_a_fresh_timer():
    clock = FakeClock()
    cb = make(clock)
    trip(cb)
    clock.advance(10)
    cb.before_call()
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    clock.advance(9)
    assert cb.state is CircuitState.OPEN  # timer restarted, not yet recovered
    clock.advance(1)
    assert cb.state is CircuitState.HALF_OPEN


def test_half_open_lets_only_the_configured_number_of_probes_through():
    clock = FakeClock()
    cb = make(clock, half_open_max_calls=1)
    trip(cb)
    clock.advance(10)
    cb.before_call()  # probe #1 in flight
    with pytest.raises(CircuitOpenError):
        cb.before_call()  # everyone else is rejected until it reports back


def test_state_changes_are_reported():
    clock = FakeClock()
    changes = []
    cb = make(clock, on_state_change=lambda name, old, new: changes.append((old.value, new.value)))
    trip(cb)
    clock.advance(10)
    cb.before_call()
    cb.record_success()
    assert changes == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


async def test_call_wraps_a_coroutine_and_counts_failures():
    cb = make(FakeClock())

    async def boom():
        raise RuntimeError("down")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(boom)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await cb.call(boom)


async def test_errors_that_are_not_failures_do_not_open_the_circuit():
    cb = make(FakeClock())

    async def not_found():
        raise LookupError("404")

    for _ in range(10):
        with pytest.raises(LookupError):
            await cb.call(not_found, is_failure=lambda exc: False)
    assert cb.state is CircuitState.CLOSED


async def test_cancelled_probe_gives_its_slot_back():
    clock = FakeClock()
    cb = make(clock, half_open_max_calls=1)
    trip(cb)
    clock.advance(10)

    class Cancelled(BaseException):
        """Stands in for task cancellation, which is a BaseException."""

    async def cancelled():
        raise Cancelled

    with pytest.raises(Cancelled):
        await cb.call(cancelled)

    cb.before_call()  # would raise CircuitOpenError if the slot had leaked
    assert cb.state is CircuitState.HALF_OPEN


def test_snapshot_reports_state():
    clock = FakeClock()
    cb = make(clock)
    assert cb.snapshot()["state"] == "closed"
    trip(cb)
    snap = cb.snapshot()
    assert snap["state"] == "open"
    assert snap["retry_in_seconds"] == pytest.approx(10.0)
