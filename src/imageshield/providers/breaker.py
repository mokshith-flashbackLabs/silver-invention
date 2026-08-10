"""The circuit-breaker state machine — pure, no I/O.

Three states, per provider::

    closed     normal. Count consecutive failures.
    open       PROVIDER_FAILURE_THRESHOLD consecutive failures (default 5).
               Stop dispatching entirely. Record the reason. Alarm.
    half_open  after the cooldown, allow exactly ONE probe.
               Success -> closed. Failure -> open, cooldown doubles to a cap.

**What counts as a failure** is the load-bearing part, and it is a deliberately
short list: timeout, 5xx, connection error, malformed response. All four reach
this module as ``status='timeout'`` or ``status='error'`` — the adapters already
classify them, including the "200 that isn't the right Hive product" case
(``search/hive.py:_parse_output``), which is a malformed response and not an
empty result.

**What does not count:**

- ``429`` — that is rate limiting, not brokenness. Back off and retry within
  bounds (:mod:`imageshield.providers.ratelimit`).
- a ``200`` with **zero matches** — the single most ordinary outcome this system
  produces. Conflating it with failure would open every breaker on a quiet week
  and stop the scans that are supposed to notice a quiet week is not normal.
- ``budget_exceeded`` / ``breaker_open`` / ``provider_disabled`` — no call was
  made, so there is nothing to judge the provider on.

The transition is computed here and applied by
:mod:`imageshield.providers.store` under a row lock, so N workers cannot each
need N failures to open one provider's breaker, and cannot each take the
half-open probe.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from imageshield.providers.models import BreakerState

# Provider call outcomes, as the breaker sees them.
Outcome = Literal["success", "failure", "neutral"]

_FAILURE_STATUSES = frozenset({"error", "timeout"})
_SUCCESS_STATUSES = frozenset({"ok"})


def classify(status: str) -> Outcome:
    """Map a ``provider_calls.status`` onto a breaker outcome.

    Anything unrecognised is ``neutral``, not ``failure``: a status this module
    has not been taught about must not be able to open a breaker as a side
    effect of being added.
    """
    if status in _SUCCESS_STATUSES:
        return "success"
    if status in _FAILURE_STATUSES:
        return "failure"
    return "neutral"


class BreakerTransition(BaseModel):
    """What to write to the provider row. ``changed`` is False when nothing
    needs writing at all, so an ordinary neutral outcome touches no row."""

    model_config = ConfigDict(frozen=True)

    changed: bool
    state: BreakerState
    consecutive_failures: int
    cooldown_seconds: int | None
    reason: str | None
    # Tri-state, and it has to be: `now` restarts the cooldown clock, `clear`
    # wipes it (a closed breaker has no opened_at, and a stale one makes the
    # cooldown maths read as though it were still open), and `keep` leaves it
    # alone. An earlier version had only a boolean, which meant every non-opening
    # transition silently NULLed breaker_opened_at — including the one that just
    # bumps a failure counter, which would have wiped the clock the cooldown is
    # measured from.
    opened_at: Literal["now", "clear", "keep"]
    # True only on the closed/half_open -> open edge. This is the alarm signal;
    # re-deriving "did it just open" from state alone would alarm on every call
    # while it stays open.
    opened: bool


def _next_cooldown(current: int | None, default: int, cap: int) -> int:
    """First open uses the default; each subsequent open doubles, up to a cap.

    The cap is not decoration. Without it a provider down over a weekend ends
    up with a cooldown longer than the outage, so recovery is never noticed and
    the breaker has to be reset by hand.
    """
    if current is None:
        return min(default, cap)
    return min(current * 2, cap)


def transition(
    *,
    state: BreakerState,
    consecutive_failures: int,
    cooldown_seconds: int | None,
    outcome: Outcome,
    failure_threshold: int,
    default_cooldown_seconds: int,
    max_cooldown_seconds: int,
    reason: str | None = None,
) -> BreakerTransition:
    if outcome == "neutral":
        if state == "half_open":
            # An inconclusive PROBE, and this case is why the half-open state
            # needs its own branch. A persistent 429 gives status
            # 'rate_limited', which classifies as neutral — correctly, since a
            # provider enforcing its rate limit is not broken. But the probe was
            # consumed, and returning `changed=False` here would leave the row
            # in 'half_open' forever: the claim query only matches 'open', so no
            # future run could ever probe again, and the provider could never
            # earn the success that closes the breaker. It would be skipped
            # until a human ran the admin reset.
            #
            # So: back to 'open' with the cooldown clock restarted, and the
            # cooldown NOT doubled. The probe told us nothing about brokenness,
            # so escalating the wait would punish the provider for a verdict we
            # never got.
            return BreakerTransition(
                changed=True,
                state="open",
                consecutive_failures=consecutive_failures,
                cooldown_seconds=cooldown_seconds,
                reason=reason or "half-open probe was inconclusive",
                opened_at="now",
                opened=False,  # not a new opening: no alarm, it was already open
            )
        return BreakerTransition(
            changed=False,
            state=state,
            consecutive_failures=consecutive_failures,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            opened_at="keep",
            opened=False,
        )

    if outcome == "success":
        # Full reset, including the cooldown: a provider that recovered has
        # earned a fresh escalation ladder. Keeping the doubled cooldown would
        # punish it for an outage it is demonstrably out of.
        return BreakerTransition(
            changed=True,
            state="closed",
            consecutive_failures=0,
            cooldown_seconds=None,
            reason=None,
            opened_at="clear",
            opened=False,
        )

    failures = consecutive_failures + 1

    if state == "open":
        # A failure recorded while the breaker is ALREADY open. Reachable
        # because each process caches provider rows for up to
        # PROVIDER_CONFIG_CACHE_SECONDS, so worker B can dispatch against a
        # 'closed' snapshot moments after worker A opened the breaker.
        #
        # Count it, and change nothing else. Re-running the open transition here
        # would double an already-doubled cooldown on every straggler call and
        # re-fire the error-level alarm each time — an alarm storm for one
        # outage, and a cooldown that races to the cap for reasons unrelated to
        # how long the provider is actually down.
        return BreakerTransition(
            changed=True,
            state="open",
            consecutive_failures=failures,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            opened_at="keep",
            opened=False,
        )

    if failures < failure_threshold:
        return BreakerTransition(
            changed=True,
            state=state,
            consecutive_failures=failures,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            opened_at="keep",
            opened=False,
        )

    # Threshold reached from 'closed', or a half-open probe failed (its failure
    # count is already at or above the threshold, which is what makes one failed
    # probe enough to re-open).
    return BreakerTransition(
        changed=True,
        state="open",
        consecutive_failures=failures,
        cooldown_seconds=_next_cooldown(
            cooldown_seconds, default_cooldown_seconds, max_cooldown_seconds
        ),
        reason=reason,
        opened_at="now",
        opened=True,
    )
