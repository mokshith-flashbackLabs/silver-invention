"""Bounded, jittered back-off for provider 429s.

The wait arithmetic is pure and unit-testable; :func:`send_with_retry` is the
one thin async driver both adapters share, so "how many times do we retry a
429" has exactly one answer in this codebase rather than one per provider —
which is how Hive ended up with one bounded retry and Google with none.

The old Lambda recurses on 429 with no attempt counter
(``weeklyInfringementScanner.js:1148``), so a persistently rate-limited
provider recurses until the Lambda times out. Read, not ported. This module is
the counter it lacks.

Three properties, each present for a specific failure it prevents:

- **Bounded** at ``PROVIDER_MAX_RETRIES``. Then give up and record
  ``rate_limited`` — which does *not* count as a breaker failure, because a
  provider enforcing its own rate limit is working correctly.
- **Honours ``retry-after``**, capped at ``PROVIDER_RETRY_MAX_WAIT_SECONDS``. A
  provider answering ``retry-after: 86400`` must not be able to pin a worker
  for a day.
- **Jittered.** N workers hitting the same rate-limited provider and all
  sleeping exactly ``retry-after`` re-collide in lockstep on the far side of
  the wait, which produces the same 429 again and burns the retry budget
  without ever spreading the load.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict

from imageshield.config import Config

# Exponential base for the fallback wait when the provider sends no
# retry-after: 1s, 2s, 4s ... capped. Small on purpose — a 429 with no header
# carries no information about how long to wait, so guessing long is as wrong
# as guessing short, and the retry count is what bounds the total.
_FALLBACK_BASE_SECONDS = 1.0


def parse_retry_after(header: str | None) -> float | None:
    """Seconds from a ``retry-after`` header, or None if absent/unparseable.

    Only the delta-seconds form is read. The HTTP-date form is legal but no
    provider this repo integrates uses it, and mis-parsing a date into a
    plausible-looking number of seconds is worse than falling back to the
    exponential default.
    """
    if header is None:
        return None
    try:
        value = float(header.strip())
    except (ValueError, AttributeError):
        return None
    if value < 0:
        return None
    return value


def wait_seconds(
    *,
    attempt: int,
    retry_after: float | None,
    max_wait_seconds: float,
    jitter_fraction: float,
    rng: Callable[[], float] = random.random,
) -> float:
    """How long to sleep before retry number ``attempt`` (0-based).

    ``rng`` is injected so tests can pin the jitter; the default is
    ``random.random``, which needs no seeding for this purpose — the point is
    de-correlating workers, not reproducibility.
    """
    base = (
        retry_after
        if retry_after is not None
        else _FALLBACK_BASE_SECONDS * (2**attempt)
    )
    capped = min(max(base, 0.0), max_wait_seconds)
    # Symmetric around `capped`, then re-clamped. Jittering only upward would
    # put the mean wait above the provider's own retry-after on every attempt,
    # which slows recovery without spreading load any better.
    spread = capped * jitter_fraction
    jittered = capped - spread + (2 * spread * rng())
    return min(max(jittered, 0.0), max_wait_seconds)


class RetryPolicy(BaseModel):
    """The 429 back-off knobs, lifted out of :class:`~imageshield.config.Config`
    so adapters take a small value object instead of the whole app config."""

    model_config = ConfigDict(frozen=True)

    max_retries: int
    max_wait_seconds: float
    jitter_fraction: float


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> tuple[httpx.Response, int]:
    """Call ``send`` until it returns a non-429, or the retry budget runs out.

    Returns ``(response, attempts)`` where ``attempts`` counts every request
    made including the first, and lands on ``provider_calls.attempt``. A final
    429 is returned as-is — the caller records ``status='rate_limited'``, which
    is deliberately *not* a breaker failure: a provider enforcing its own rate
    limit is working correctly.

    httpx exceptions propagate. Timeouts and connection errors are the adapter's
    to classify (``timeout`` vs ``error``), and retrying them here would turn one
    slow provider into ``PROVIDER_TIMEOUT_SECONDS x (max_retries + 1)`` of held
    worker time on a run that is already going to fail.
    """
    attempts = 0
    while True:
        response = await send()
        attempts += 1
        if response.status_code != 429 or attempts > policy.max_retries:
            return response, attempts
        await sleep(
            wait_seconds(
                attempt=attempts - 1,
                retry_after=parse_retry_after(response.headers.get("retry-after")),
                max_wait_seconds=policy.max_wait_seconds,
                jitter_fraction=policy.jitter_fraction,
                rng=rng,
            )
        )


def policy_from_config(cfg: Config) -> RetryPolicy:
    return RetryPolicy(
        max_retries=cfg.provider_max_retries,
        max_wait_seconds=cfg.provider_retry_max_wait_seconds,
        jitter_fraction=cfg.provider_retry_jitter_fraction,
    )
