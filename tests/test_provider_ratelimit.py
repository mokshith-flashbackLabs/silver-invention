"""Bounded, jittered 429 back-off.

The bound is the point. The old Lambda recursed on 429 with no attempt counter
(weeklyInfringementScanner.js:1148) and a persistently limited provider recursed
until the Lambda timed out.
"""

from __future__ import annotations

import httpx
import pytest

from imageshield.providers.ratelimit import (
    RetryPolicy,
    parse_retry_after,
    send_with_retry,
    wait_seconds,
)

POLICY = RetryPolicy(max_retries=3, max_wait_seconds=30.0, jitter_fraction=0.25)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("5", 5.0),
        (" 5.5 ", 5.5),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("not-a-number", None),
        # The HTTP-date form is legal but unused by our providers, and
        # mis-parsing it into a plausible number of seconds is worse than
        # falling back to the exponential default.
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("-1", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_retry_after_is_honoured_and_capped() -> None:
    # Jitter pinned to the midpoint so the arithmetic is the only variable.
    at_mid = wait_seconds(
        attempt=0,
        retry_after=10.0,
        max_wait_seconds=30.0,
        jitter_fraction=0.25,
        rng=lambda: 0.5,
    )
    assert at_mid == pytest.approx(10.0)

    # A provider answering `retry-after: 86400` must not pin a worker for a day.
    capped = wait_seconds(
        attempt=0,
        retry_after=86400.0,
        max_wait_seconds=30.0,
        jitter_fraction=0.0,
        rng=lambda: 0.5,
    )
    assert capped == pytest.approx(30.0)


def test_jitter_spreads_both_ways_around_the_base() -> None:
    """Symmetric, not upward-only: jittering only up would put the mean wait
    above the provider's own retry-after on every attempt."""
    low = wait_seconds(
        attempt=0, retry_after=10.0, max_wait_seconds=30.0,
        jitter_fraction=0.25, rng=lambda: 0.0,
    )
    high = wait_seconds(
        attempt=0, retry_after=10.0, max_wait_seconds=30.0,
        jitter_fraction=0.25, rng=lambda: 1.0,
    )
    assert low == pytest.approx(7.5)
    assert high == pytest.approx(12.5)


def test_no_retry_after_backs_off_exponentially() -> None:
    waits = [
        wait_seconds(
            attempt=i, retry_after=None, max_wait_seconds=30.0,
            jitter_fraction=0.0, rng=lambda: 0.5,
        )
        for i in range(4)
    ]
    assert waits == pytest.approx([1.0, 2.0, 4.0, 8.0])


def _client(statuses: list[int]) -> tuple[httpx.AsyncClient, list[int]]:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[len(seen)] if len(seen) < len(statuses) else statuses[-1]
        seen.append(status)
        return httpx.Response(status, headers={"retry-after": "0"}, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_retries_stop_at_max_retries_and_report_the_attempt_count() -> None:
    client, seen = _client([429])
    slept: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        slept.append(seconds)

    response, attempts = await send_with_retry(
        lambda: client.get("https://p.test/x"), POLICY, sleep=_no_sleep
    )

    # 1 initial + 3 retries = 4 requests, then give up.
    assert attempts == 4
    assert len(seen) == 4
    assert len(slept) == 3
    assert response.status_code == 429
    await client.aclose()


async def test_a_non_429_returns_immediately() -> None:
    client, seen = _client([200])
    response, attempts = await send_with_retry(lambda: client.get("https://p.test/x"), POLICY)
    assert (response.status_code, attempts, len(seen)) == (200, 1, 1)
    await client.aclose()


async def test_a_429_then_success_reports_two_attempts() -> None:
    client, _ = _client([429, 200])

    async def _no_sleep(_seconds: float) -> None:
        return None

    response, attempts = await send_with_retry(
        lambda: client.get("https://p.test/x"), POLICY, sleep=_no_sleep
    )
    assert (response.status_code, attempts) == (200, 2)
    await client.aclose()


async def test_zero_max_retries_makes_exactly_one_request() -> None:
    client, seen = _client([429])
    _, attempts = await send_with_retry(
        lambda: client.get("https://p.test/x"),
        RetryPolicy(max_retries=0, max_wait_seconds=30.0, jitter_fraction=0.25),
    )
    assert (attempts, len(seen)) == (1, 1)
    await client.aclose()
