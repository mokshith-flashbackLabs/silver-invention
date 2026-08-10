"""The circuit-breaker state machine, pure.

The two classification rules are the ones worth pinning hardest, because
getting either wrong is silent: a 429 opening the breaker takes a healthy
provider out of rotation, and a zero-match 200 opening it takes EVERY provider
out on a quiet week — which is exactly the week a safety product must keep
looking.
"""

from __future__ import annotations

import pytest

from imageshield.providers.breaker import classify, transition

THRESHOLD = 5
COOLDOWN = 300
CAP = 3600


def _t(
    *,
    state: str = "closed",
    failures: int = 0,
    cooldown: int | None = None,
    outcome: str = "failure",
):  # type: ignore[no-untyped-def]
    return transition(
        state=state,  # type: ignore[arg-type]
        consecutive_failures=failures,
        cooldown_seconds=cooldown,
        outcome=outcome,  # type: ignore[arg-type]
        failure_threshold=THRESHOLD,
        default_cooldown_seconds=COOLDOWN,
        max_cooldown_seconds=CAP,
        reason="timeout",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", "success"),
        ("error", "failure"),
        ("timeout", "failure"),
        # 429 is rate limiting, not brokenness.
        ("rate_limited", "neutral"),
        # No call was made — nothing to judge the provider on.
        ("budget_exceeded", "neutral"),
        ("breaker_open", "neutral"),
        ("provider_disabled", "neutral"),
        # A status this module was never taught must not open a breaker as a
        # side effect of being added.
        ("something_new", "neutral"),
    ],
)
def test_classification(status: str, expected: str) -> None:
    assert classify(status) == expected


def test_a_zero_match_success_is_a_success_not_a_failure() -> None:
    """The most ordinary result this system produces. `ok` with no matches is
    indistinguishable here from `ok` with fifty — the breaker only ever sees
    the status, so there is no path by which "found nothing" becomes a fault."""
    assert classify("ok") == "success"


def test_five_consecutive_failures_open_the_breaker_and_four_do_not() -> None:
    for failures in range(THRESHOLD - 1):
        change = _t(failures=failures)
        assert change.state == "closed"
        assert change.opened is False
        assert change.consecutive_failures == failures + 1

    fifth = _t(failures=THRESHOLD - 1)
    assert fifth.state == "open"
    assert fifth.opened is True
    assert fifth.consecutive_failures == THRESHOLD
    assert fifth.opened_at == "now"
    assert fifth.cooldown_seconds == COOLDOWN
    assert fifth.reason == "timeout"


def test_neutral_outcomes_touch_nothing() -> None:
    """A rate-limited or skipped provider must leave the counter exactly where
    it was — not reset it either, since resetting would let a provider alternate
    429/timeout forever without ever reaching the threshold."""
    change = _t(state="closed", failures=3, outcome="neutral")
    assert change.changed is False
    assert change.consecutive_failures == 3
    assert change.state == "closed"
    assert change.opened_at == "keep"


def test_an_inconclusive_probe_returns_to_open_rather_than_wedging() -> None:
    """The wedge this exists to prevent: a half-open probe that comes back 429.

    `rate_limited` classifies as neutral — correctly, a provider enforcing its
    own rate limit is not broken. But the probe was spent. If neutral simply
    changed nothing, the row would stay 'half_open' forever: the claim query only
    matches 'open', so no later run could probe again, the provider could never
    earn the success that closes the breaker, and every run would skip it until a
    human ran the admin reset. Permanent partial coverage, waiting on somebody
    noticing.

    Back to 'open' with the clock restarted, and the cooldown NOT doubled: the
    probe returned no verdict about brokenness, so there is nothing to escalate.
    """
    change = _t(state="half_open", failures=THRESHOLD, cooldown=COOLDOWN, outcome="neutral")

    assert change.changed is True
    assert change.state == "open"
    assert change.cooldown_seconds == COOLDOWN  # restarted, not doubled
    assert change.opened_at == "now"  # the cooldown clock restarts
    assert change.opened is False  # already open: no second alarm
    assert change.consecutive_failures == THRESHOLD


def test_a_failure_while_already_open_neither_doubles_nor_realarms() -> None:
    """Reachable through the runtime cache: worker B can still hold a 'closed'
    snapshot for up to PROVIDER_CONFIG_CACHE_SECONDS after worker A opened the
    breaker, so its in-flight call lands here against an open row.

    Count it, change nothing else. Re-running the open transition would double an
    already-doubled cooldown on every straggler and re-fire the error-level alarm
    each time — an alarm storm for one outage, and a cooldown racing to the cap
    for reasons unrelated to how long the provider is actually down.
    """
    change = _t(state="open", failures=THRESHOLD + 2, cooldown=2 * COOLDOWN)

    assert change.state == "open"
    assert change.consecutive_failures == THRESHOLD + 3  # counted
    assert change.cooldown_seconds == 2 * COOLDOWN  # NOT doubled again
    assert change.opened is False  # NOT re-alarmed
    assert change.opened_at == "keep"  # clock not restarted


def test_the_opened_at_tristate_never_clears_the_clock_by_accident() -> None:
    """`opened_at` has to be three-valued. It was a bool, and the SQL read it as
    "set now() or else NULL" — so every non-opening transition silently wiped
    breaker_opened_at, including the one that merely bumps a failure counter.
    That is the column the cooldown is measured from, so wiping it disabled the
    half-open claim entirely.
    """
    assert _t(state="closed", failures=1).opened_at == "keep"  # counter bump
    assert _t(state="closed", failures=THRESHOLD - 1).opened_at == "now"  # opens
    assert _t(outcome="success").opened_at == "clear"  # closed has no clock


def test_success_fully_resets_including_the_cooldown_ladder() -> None:
    change = _t(state="half_open", failures=7, cooldown=1200, outcome="success")
    assert change.state == "closed"
    assert change.consecutive_failures == 0
    assert change.cooldown_seconds is None
    assert change.reason is None
    assert change.opened_at == "clear"
    assert change.opened is False


def test_a_failed_half_open_probe_reopens_with_a_doubled_cooldown() -> None:
    change = _t(state="half_open", failures=THRESHOLD, cooldown=COOLDOWN)
    assert change.state == "open"
    assert change.opened is True
    assert change.cooldown_seconds == 2 * COOLDOWN


def test_the_doubling_is_capped() -> None:
    """Without the cap, a provider down over a weekend ends up with a cooldown
    longer than the outage, so recovery is never noticed."""
    change = _t(state="half_open", failures=THRESHOLD, cooldown=CAP)
    assert change.cooldown_seconds == CAP

    nearly = _t(state="half_open", failures=THRESHOLD, cooldown=CAP - 1)
    assert nearly.cooldown_seconds == CAP


def test_a_cooldown_cap_below_the_default_still_holds() -> None:
    change = transition(
        state="closed",
        consecutive_failures=THRESHOLD - 1,
        cooldown_seconds=None,
        outcome="failure",
        failure_threshold=THRESHOLD,
        default_cooldown_seconds=600,
        max_cooldown_seconds=60,
        reason="timeout",
    )
    assert change.cooldown_seconds == 60
