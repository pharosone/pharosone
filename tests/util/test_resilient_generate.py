"""Resilience for LLM calls: `resilient_generate` time-bounds every `model.generate`, retries on
timeout + transient errors with deterministic bounded backoff, and RAISES `ResilientGenerateError`
on exhaustion so callers' existing fallbacks run — it must NEVER hang. All offline, fast via tiny
timeouts; model.generate is stubbed (no network). Backoff `asyncio.sleep` is monkeypatched to a
no-op so the retry tests stay near-instant and assert deterministic delays."""
import asyncio

import pytest

from probe_engine.util.resilient_generate import (
    ResilientGenerateError,
    is_transient_error,
    resilient_generate,
)


class _Out:
    def __init__(self, completion="ok"):
        self.completion = completion


class _HangModel:
    """generate() never returns — the live failure mode (provider throttling)."""

    def __init__(self):
        self.calls = 0

    async def generate(self, _messages, **_kw):
        self.calls += 1
        await asyncio.Event().wait()  # blocks forever


class _FlakyModel:
    """Raises a given error the first `fail_times` calls, then returns successfully."""

    def __init__(self, error, fail_times):
        self._error = error
        self._fail_times = fail_times
        self.calls = 0

    async def generate(self, _messages, **_kw):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return _Out("recovered")


class _BoomModel:
    def __init__(self, error):
        self._error = error
        self.calls = 0

    async def generate(self, _messages, **_kw):
        self.calls += 1
        raise self._error


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff instant + record the requested delays (assert determinism, no randomness)."""
    delays = []

    async def _fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return delays


# (a) a model whose generate sleeps forever -> resilient_generate RAISES (bounded), never hangs.
def test_hang_raises_bounded_not_forever(no_sleep):
    model = _HangModel()

    async def _go():
        return await resilient_generate(
            model, "p", timeout_s=0.01, max_retries=2, base_backoff=0.0
        )

    with pytest.raises(ResilientGenerateError) as ei:
        asyncio.run(asyncio.wait_for(_go(), timeout=5.0))  # outer guard: if it hangs, test fails
    assert isinstance(ei.value.last_error, asyncio.TimeoutError)
    assert ei.value.attempts == 3  # max_retries(2) + 1
    assert model.calls == 3  # every attempt actually hit the (timing-out) model


# (b) transient error N times then success -> retried and succeeds.
def test_transient_then_success(no_sleep):
    model = _FlakyModel(RuntimeError("429 Too Many Requests"), fail_times=2)
    out = asyncio.run(
        resilient_generate(model, "p", timeout_s=1.0, max_retries=3, base_backoff=1.0)
    )
    assert out.completion == "recovered"
    assert model.calls == 3  # 2 failures + 1 success
    # deterministic exponential backoff, no jitter: base*2^0, base*2^1
    assert no_sleep == [1.0, 2.0]


def test_timeout_then_success_retries(no_sleep):
    # First two attempts time out (hang), third returns. Use a model that hangs N times then yields.
    class _HangThenOk:
        def __init__(self, hangs):
            self.hangs = hangs
            self.calls = 0

        async def generate(self, _m, **_k):
            self.calls += 1
            if self.calls <= self.hangs:
                await asyncio.Event().wait()
            return _Out("late")

    model = _HangThenOk(2)
    out = asyncio.run(
        resilient_generate(model, "p", timeout_s=0.01, max_retries=3, base_backoff=0.0)
    )
    assert out.completion == "late"
    assert model.calls == 3


# (b') exhausting transient retries -> raises.
def test_transient_exhausted_raises(no_sleep):
    model = _FlakyModel(RuntimeError("503 service unavailable"), fail_times=99)
    with pytest.raises(ResilientGenerateError) as ei:
        asyncio.run(resilient_generate(model, "p", timeout_s=1.0, max_retries=2, base_backoff=0.5))
    assert model.calls == 3
    assert ei.value.attempts == 3
    assert no_sleep == [0.5, 1.0]


def test_non_transient_not_retried(no_sleep):
    # A non-transient (e.g. malformed-request/auth) error is NOT retried — raised immediately wrapped.
    model = _BoomModel(ValueError("invalid model parameter foo"))
    with pytest.raises(ResilientGenerateError):
        asyncio.run(resilient_generate(model, "p", timeout_s=1.0, max_retries=5, base_backoff=1.0))
    assert model.calls == 1  # no retries burned
    assert no_sleep == []


def test_success_first_try_no_backoff(no_sleep):
    out = asyncio.run(resilient_generate(_FlakyModel(RuntimeError("x"), 0), "p", timeout_s=1.0))
    assert out.completion == "recovered"
    assert no_sleep == []


def test_backoff_is_capped(no_sleep):
    model = _FlakyModel(RuntimeError("connection reset"), fail_times=99)
    with pytest.raises(ResilientGenerateError):
        asyncio.run(
            resilient_generate(
                model, "p", timeout_s=1.0, max_retries=4, base_backoff=10.0, max_backoff=15.0
            )
        )
    # 10, 20->cap 15, 40->cap 15, 80->cap 15
    assert no_sleep == [10.0, 15.0, 15.0, 15.0]


def test_generate_kwargs_passed_through(no_sleep):
    seen = {}

    class _M:
        async def generate(self, messages, **kw):
            seen["messages"] = messages
            seen["kw"] = kw
            return _Out()

    asyncio.run(
        resilient_generate(_M(), ["m"], timeout_s=1.0, generate_kwargs={"cache": True})
    )
    assert seen["messages"] == ["m"]
    assert seen["kw"] == {"cache": True}


def test_invalid_args():
    with pytest.raises(ValueError):
        asyncio.run(resilient_generate(_HangModel(), "p", timeout_s=0))
    with pytest.raises(ValueError):
        asyncio.run(resilient_generate(_HangModel(), "p", max_retries=-1))


@pytest.mark.parametrize(
    "err,expected",
    [
        (asyncio.TimeoutError(), True),
        (RuntimeError("429 Too Many Requests"), True),
        (RuntimeError("503 Service Unavailable"), True),
        (RuntimeError("Connection reset by peer"), True),
        (RuntimeError("rate limit exceeded"), True),
        (RuntimeError("overloaded_error"), True),
        (ValueError("invalid request: bad field"), False),
        (KeyError("missing"), False),
    ],
)
def test_is_transient_error_classification(err, expected):
    assert is_transient_error(err) is expected
