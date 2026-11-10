import pytest

from app.services.retry import RetryPolicy, retry_async

pytestmark = pytest.mark.anyio


class Transient(Exception):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("transient")
        self.retry_after = retry_after


class Permanent(Exception):
    pass


class FakeTime:
    """Virtual clock: ``sleep`` advances time instantly, so tests never really wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def flaky(failures: int, error: Exception | None = None):
    """Async function that fails ``failures`` times, then returns 'ok'."""
    calls = {"n": 0}

    async def fn(_attempt: int) -> str:
        calls["n"] += 1
        if calls["n"] <= failures:
            raise error or Transient()
        return "ok"

    return fn, calls


def retry_transient(exc: Exception) -> bool:
    return isinstance(exc, Transient)


async def test_success_on_first_attempt_does_not_sleep():
    t = FakeTime()
    fn, calls = flaky(0)
    result = await retry_async(
        fn, policy=RetryPolicy(jitter=False), is_retryable=retry_transient, sleep=t.sleep, clock=t
    )
    assert result == "ok"
    assert calls["n"] == 1
    assert t.sleeps == []


async def test_retries_with_exponential_backoff_then_succeeds():
    t = FakeTime()
    fn, calls = flaky(2)
    policy = RetryPolicy(max_attempts=4, base_delay=0.1, max_delay=10, jitter=False)
    result = await retry_async(fn, policy=policy, is_retryable=retry_transient, sleep=t.sleep, clock=t)
    assert result == "ok"
    assert calls["n"] == 3
    assert t.sleeps == pytest.approx([0.1, 0.2])


async def test_gives_up_after_max_attempts_and_raises_last_error():
    t = FakeTime()
    fn, calls = flaky(99)
    policy = RetryPolicy(max_attempts=3, base_delay=0.1, jitter=False)
    with pytest.raises(Transient):
        await retry_async(fn, policy=policy, is_retryable=retry_transient, sleep=t.sleep, clock=t)
    assert calls["n"] == 3
    assert len(t.sleeps) == 2  # no sleep after the final attempt


async def test_non_retryable_error_fails_immediately():
    t = FakeTime()
    fn, calls = flaky(99, error=Permanent())
    with pytest.raises(Permanent):
        await retry_async(
            fn, policy=RetryPolicy(max_attempts=5), is_retryable=retry_transient, sleep=t.sleep, clock=t
        )
    assert calls["n"] == 1
    assert t.sleeps == []


def test_backoff_is_capped_at_max_delay():
    policy = RetryPolicy(base_delay=1.0, max_delay=3.0, multiplier=2.0, jitter=False)
    assert [policy.backoff(n) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_full_jitter_stays_within_zero_and_ceiling():
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=True)
    assert policy.backoff(3, rng=lambda: 0.0) == 0.0
    assert policy.backoff(3, rng=lambda: 0.5) == pytest.approx(2.0)  # ceiling for attempt 3 is 4.0
    assert policy.backoff(3, rng=lambda: 1.0) == pytest.approx(4.0)


async def test_honours_retry_after_when_longer_than_backoff():
    t = FakeTime()
    fn, _ = flaky(1, error=Transient(retry_after=2.0))
    policy = RetryPolicy(max_attempts=3, base_delay=0.1, jitter=False)
    await retry_async(fn, policy=policy, is_retryable=retry_transient, sleep=t.sleep, clock=t)
    assert t.sleeps == [2.0]


async def test_stops_when_next_wait_would_exceed_deadline():
    t = FakeTime()
    fn, calls = flaky(99)
    policy = RetryPolicy(max_attempts=10, base_delay=0.1, jitter=False)
    # Budget 0.15s: first wait (0.1) fits, second wait (0.2) would overshoot -> give up.
    with pytest.raises(Transient):
        await retry_async(
            fn, policy=policy, is_retryable=retry_transient, deadline=0.15, sleep=t.sleep, clock=t
        )
    assert calls["n"] == 2
    assert t.sleeps == pytest.approx([0.1])


async def test_reports_every_attempt_and_retry():
    t = FakeTime()
    fn, _ = flaky(2)
    attempts, retries = [], []
    await retry_async(
        fn,
        policy=RetryPolicy(max_attempts=4, base_delay=0.1, jitter=False),
        is_retryable=retry_transient,
        on_attempt=attempts.append,
        on_retry=lambda n, delay, exc: retries.append((n, delay)),
        sleep=t.sleep,
        clock=t,
    )
    assert [a.number for a in attempts] == [1, 2, 3]
    assert [a.success for a in attempts] == [False, False, True]
    assert attempts[2].result == "ok"
    assert isinstance(attempts[0].error, Transient)
    assert retries == [(1, pytest.approx(0.1)), (2, pytest.approx(0.2))]
