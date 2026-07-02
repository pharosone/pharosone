"""Resilient wrapper around an Inspect ``model.generate`` call (ITEM 2).

A throttling/slow provider must NEVER hang a run. The variation paraphrase mutator and the LLM
judge both already have graceful FALLBACKS on failure (paraphrase -> deterministic
``techniques.diversify``; judge -> UNVERIFIED), but a call that *hangs forever* never reaches the
fallback. This module makes every model call:

  * **time-bounded** — each attempt is wrapped in ``asyncio.wait_for(timeout_s)`` so a stuck
    provider cannot block past the timeout;
  * **retried with bounded exponential backoff** — on ``asyncio.TimeoutError`` and *transient*
    provider errors (rate-limit / 429, 5xx, connection / transport / timeout errors), with
    DETERMINISTIC delays (``base_backoff * 2**attempt``, capped) via ``asyncio.sleep`` — no
    randomness/jitter, so tests are reproducible;
  * **failure-typed** — after exhausting retries it RAISES :class:`ResilientGenerateError`, which
    the caller's EXISTING ``except`` fallback path catches. It NEVER returns by hanging.

Non-transient errors (e.g. a malformed-request / auth error) are raised immediately wrapped in
:class:`ResilientGenerateError` — retrying them would only waste budget; the caller still falls
back, just without burning the retry budget on an error that cannot succeed.

The wrapper is intentionally tiny and dependency-free (only ``asyncio``) so it can sit under both
``variation`` and ``scoring`` without creating an import cycle.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, Sequence

__all__ = ["ResilientGenerateError", "resilient_generate", "is_transient_error"]


class ResilientGenerateError(RuntimeError):
    """Raised when ``resilient_generate`` gives up (timeout/transient retries exhausted, or a
    non-transient error). The caller's existing fallback path is expected to catch this (it is a
    plain ``Exception`` subclass) and degrade gracefully — paraphrase -> diversify, judge ->
    UNVERIFIED. It carries the last underlying error as ``__cause__`` and ``.last_error``."""

    def __init__(self, message: str, *, last_error: BaseException | None = None, attempts: int = 0):
        super().__init__(message)
        self.last_error = last_error
        self.attempts = attempts


# Substrings that, when present in an error's type name or message, mark it TRANSIENT (worth a
# retry): provider throttling, gateway/server errors, and connection/transport hiccups. Matched
# case-insensitively against both ``type(err).__name__`` and ``str(err)`` so it works regardless of
# which provider SDK raised — we never import those SDKs here.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "ratelimit",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "throttl",
    "timeout",
    "timed out",
    "503",
    "502",
    "500",
    "504",
    "internalservererror",
    "internal server error",
    "service unavailable",
    "serviceunavailable",
    "badgateway",
    "bad gateway",
    "gatewaytimeout",
    "gateway timeout",
    "connection",
    "connect",
    "transport",
    "econnreset",
    "reset by peer",
    "broken pipe",
    "temporarily unavailable",
    "apiconnectionerror",
    "apitimeouterror",
)


def is_transient_error(err: BaseException) -> bool:
    """Heuristically decide whether ``err`` is worth retrying.

    ``asyncio.TimeoutError`` (our own per-attempt timeout, and providers' own timeouts) is always
    transient. Otherwise we match a curated set of substrings against the exception's *type name*
    and *message* — string-based on purpose so we never depend on importing httpx/openai/etc."""
    if isinstance(err, asyncio.TimeoutError):
        return True
    # asyncio.CancelledError must NOT be swallowed/retried — it means the task is being torn down.
    if isinstance(err, asyncio.CancelledError):  # pragma: no cover - defensive
        return False
    hay = (type(err).__name__ + " " + str(err)).lower()
    return any(marker in hay for marker in _TRANSIENT_MARKERS)


async def resilient_generate(
    model: Any,
    messages: Sequence[Any] | str,
    *,
    timeout_s: float = 60.0,
    max_retries: int = 3,
    base_backoff: float = 1.0,
    max_backoff: float = 30.0,
    generate_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Call ``await model.generate(messages, **kw)`` resiliently and return its output.

    Each attempt is bounded by ``asyncio.wait_for(timeout_s)``. On timeout or a *transient* error
    the call is retried up to ``max_retries`` extra times (so up to ``max_retries + 1`` attempts
    total) with deterministic exponential backoff (``min(base_backoff * 2**attempt, max_backoff)``
    seconds via ``asyncio.sleep``; NO jitter). A non-transient error is NOT retried. When attempts
    are exhausted — or on the first non-transient error — :class:`ResilientGenerateError` is raised
    so the caller's existing fallback runs. This function never hangs: total wall time is bounded by
    the sum of the per-attempt timeouts plus the backoff delays.

    ``messages`` is passed straight through (Inspect ``model.generate`` accepts a message list or a
    plain prompt string), as are any ``generate_kwargs``.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    kw = dict(generate_kwargs or {})
    last_error: BaseException | None = None
    attempts = max_retries + 1

    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(model.generate(messages, **kw), timeout=timeout_s)
        except asyncio.CancelledError:  # pragma: no cover - never swallow cancellation
            raise
        except BaseException as err:  # noqa: BLE001 - we classify, then re-raise as a typed error
            last_error = err
            transient = is_transient_error(err)
            is_last = attempt >= attempts - 1
            if not transient:
                raise ResilientGenerateError(
                    f"non-transient error from model.generate: {type(err).__name__}",
                    last_error=err,
                    attempts=attempt + 1,
                ) from err
            if is_last:
                break
            delay = min(base_backoff * (2 ** attempt), max_backoff)
            if delay > 0:
                await asyncio.sleep(delay)

    raise ResilientGenerateError(
        f"model.generate failed after {attempts} attempt(s); "
        f"last error: {type(last_error).__name__ if last_error else 'unknown'}",
        last_error=last_error,
        attempts=attempts,
    ) from last_error
