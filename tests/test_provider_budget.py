"""The budget verdict and headroom, pure."""

from __future__ import annotations

from decimal import Decimal

from imageshield.providers.budget import headroom, verdict

ZERO = Decimal("0")


def test_no_budget_configured_is_allowed() -> None:
    """The state both providers ship in. The cap is opt-in per provider."""
    v = verdict(
        cost_per_call_usd=Decimal("0.0035"),
        daily_budget_usd=None,
        spent_today_usd=Decimal("999999"),
    )
    assert v.allowed is True


def test_budget_is_a_ceiling_the_spend_may_reach_but_not_exceed() -> None:
    exactly = verdict(
        cost_per_call_usd=Decimal("1.00"),
        daily_budget_usd=Decimal("10.00"),
        spent_today_usd=Decimal("9.00"),
    )
    assert exactly.allowed is True  # 9 + 1 == 10: allowed

    over = verdict(
        cost_per_call_usd=Decimal("1.00"),
        daily_budget_usd=Decimal("10.00"),
        spent_today_usd=Decimal("9.01"),
    )
    assert over.allowed is False  # 9.01 + 1 > 10: refused
    assert "10.00" in over.detail  # the detail names the budget it hit


def test_a_budget_with_an_unknown_cost_fails_CLOSED() -> None:
    """The case that matters for hive, whose contract price is not in this repo.

    Failing open here would mean the one provider nobody could price is the one
    provider with no ceiling — exactly backwards, and silent until an invoice.
    """
    v = verdict(
        cost_per_call_usd=None,
        daily_budget_usd=Decimal("10.00"),
        spent_today_usd=ZERO,
    )
    assert v.allowed is False
    assert "cost_per_call_usd is unknown" in v.detail


def test_an_unknown_cost_with_no_budget_is_still_allowed() -> None:
    """No cap asked for, none enforced — the pre-step-8 behaviour, unchanged."""
    v = verdict(cost_per_call_usd=None, daily_budget_usd=None, spent_today_usd=ZERO)
    assert v.allowed is True


def test_headroom_distinguishes_unlimited_from_exhausted() -> None:
    assert headroom(daily_budget_usd=None, spent_today_usd=Decimal("5")) is None
    assert headroom(
        daily_budget_usd=Decimal("10"), spent_today_usd=Decimal("4")
    ) == Decimal("6")
    # Never negative: an overshoot (bounded, see providers/gate.py) reads as
    # zero headroom rather than as a negative budget.
    assert headroom(
        daily_budget_usd=Decimal("10"), spent_today_usd=Decimal("12")
    ) == ZERO
