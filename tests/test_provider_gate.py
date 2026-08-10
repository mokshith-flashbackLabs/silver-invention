"""The dispatch guard chain: ENABLED -> BREAKER -> BUDGET -> DISPATCH.

The ORDER is what these tests pin, not just the individual verdicts. The cheapest
and most absolute checks have to come first, so a provider that is switched off
never costs a budget read, and an open breaker never costs one either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from imageshield.providers.gate import decide
from imageshield.providers.models import Dispatch, Skip
from tests.providers_fakes import HIVE, FakeControlStore, runtime

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


async def test_a_healthy_provider_dispatches_with_its_cost_attached() -> None:
    control = FakeControlStore({HIVE: runtime(HIVE, cost="0.0035")})

    decision = await decide(HIVE, runtime=runtime(HIVE, cost="0.0035"), store=control, now=NOW)

    assert isinstance(decision, Dispatch)
    # The cost travels with the decision so the recording path cannot charge a
    # different figure from the one the guard checked against.
    assert decision.cost_usd == Decimal("0.0035")
    assert decision.probe is False


async def test_a_missing_provider_row_is_treated_as_disabled_not_an_error() -> None:
    """A row deleted between run creation and dispatch must not fail the run."""
    control = FakeControlStore({})

    decision = await decide(HIVE, runtime=None, store=control, now=NOW)

    assert isinstance(decision, Skip)
    assert decision.reason == "provider_disabled"


async def test_the_kill_switch_short_circuits_before_the_budget_read() -> None:
    """ENABLED is step 2 and BUDGET is step 4. A disabled provider whose budget
    is exhausted must report the kill switch, not the budget — the operator's
    deliberate action is the more useful answer, and it is also the cheaper
    check."""
    control = FakeControlStore(spend={HIVE: "999.00"})
    disabled = runtime(HIVE, enabled=False, cost="1.00", daily_budget="10.00")

    decision = await decide(HIVE, runtime=disabled, store=control, now=NOW)

    assert isinstance(decision, Skip)
    assert decision.reason == "provider_disabled"
    assert control.half_open_claims == []  # no breaker work either


async def test_an_open_breaker_that_loses_the_claim_skips_as_breaker_open() -> None:
    control = FakeControlStore(half_open_grants=set())
    tripped = runtime(
        HIVE,
        breaker_state="open",
        breaker_opened_at=NOW,
        failures=5,
        cooldown=300,
    )

    decision = await decide(HIVE, runtime=tripped, store=control, now=NOW)

    assert isinstance(decision, Skip)
    assert decision.reason == "breaker_open"


async def test_a_budget_refusal_never_consumes_the_half_open_probe() -> None:
    """The ordering rule, applied inside the chain.

    The printed chain is ENABLED -> BREAKER -> BUDGET, and claiming the probe at
    the BREAKER step reads as the obvious implementation. It is a bug: the claim
    is a durable write (breaker_state='half_open'), so a budget refusal on the
    next step returns a Skip having already burned the single probe. Nothing
    releases it on that path, so the breaker sits in half_open — a provider
    skipped on every subsequent run, and before the stale-probe reclaim existed,
    skipped until a human ran the admin reset.

    A cheaper, more absolute refusal must never consume a scarcer resource. The
    probe is claimed last, immediately before dispatch.
    """
    control = FakeControlStore(spend={HIVE: "10.00"}, half_open_grants={HIVE})
    cooled_but_broke = runtime(
        HIVE,
        cost="1.00",
        daily_budget="10.00",  # already at the cap
        breaker_state="open",
        breaker_opened_at=NOW - timedelta(seconds=600),  # cooled: would win a claim
        failures=5,
        cooldown=300,
    )

    decision = await decide(HIVE, runtime=cooled_but_broke, store=control, now=NOW)

    assert isinstance(decision, Skip)
    assert decision.reason == "budget_exceeded"
    assert control.half_open_claims == []  # the probe survives for a run that can pay


async def test_half_open_in_flight_elsewhere_skips_rather_than_double_probing() -> None:
    """"Allow ONE probe" is meaningless across N workers if each of them treats
    half_open as permission."""
    control = FakeControlStore()

    decision = await decide(
        HIVE, runtime=runtime(HIVE, breaker_state="half_open", failures=5), store=control, now=NOW
    )

    assert isinstance(decision, Skip)
    assert decision.reason == "breaker_open"
    assert "in flight" in decision.detail
    assert control.half_open_claims == []  # no second claim attempted


async def test_a_won_half_open_claim_dispatches_and_is_marked_as_the_probe() -> None:
    control = FakeControlStore(half_open_grants={HIVE})
    cooled = runtime(
        HIVE,
        breaker_state="open",
        breaker_opened_at=NOW - timedelta(seconds=600),
        failures=5,
        cooldown=300,
    )

    decision = await decide(HIVE, runtime=cooled, store=control, now=NOW)

    assert isinstance(decision, Dispatch)
    assert decision.probe is True
    assert control.half_open_claims == [HIVE]


async def test_budget_exhaustion_skips_with_the_budget_reason() -> None:
    control = FakeControlStore(spend={HIVE: "10.00"})

    decision = await decide(
        HIVE,
        runtime=runtime(HIVE, cost="1.00", daily_budget="10.00"),
        store=control,
        now=NOW,
    )

    assert isinstance(decision, Skip)
    assert decision.reason == "budget_exceeded"


async def test_no_spend_row_yet_reads_as_zero_not_unknown() -> None:
    """The provider_spend row is created by the first call, so its absence
    genuinely means no calls today."""
    control = FakeControlStore(spend={})

    decision = await decide(
        HIVE,
        runtime=runtime(HIVE, cost="1.00", daily_budget="1.00"),
        store=control,
        now=NOW,
    )

    assert isinstance(decision, Dispatch)
