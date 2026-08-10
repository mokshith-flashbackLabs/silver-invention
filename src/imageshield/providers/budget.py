"""The budget verdict — pure, no I/O.

Enforced **before** dispatch. Checking after the call means the money is
already spent; the whole point is the check that prevents the spend.

The input is one pre-aggregated ``provider_spend`` row for today, never a
``SUM`` over ``provider_calls``. That table grows with every call ever made, so
a guard built on it would get slower in proportion to how much has been spent —
the cost check would itself become a cost.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

_ZERO = Decimal("0")


class BudgetVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    detail: str


def verdict(
    *,
    cost_per_call_usd: Decimal | None,
    daily_budget_usd: Decimal | None,
    spent_today_usd: Decimal,
) -> BudgetVerdict:
    """Decide whether one more call fits inside today's budget.

    Three cases, and the third is the one worth stating out loud:

    1. **No budget configured** -> allowed. This is the pre-step-8 world and
       the state both providers ship in; the cap is opt-in per provider.
    2. **Budget configured, cost known** -> allowed iff
       ``spent + cost <= budget``. Strictly greater-than is refused, so the
       budget is a ceiling the spend may reach and not exceed.
    3. **Budget configured, cost UNKNOWN** -> refused. An operator who asked
       for a spend cap must not get unbounded spend because we cannot price the
       calls. Failing open here would mean the one provider whose contract
       price nobody filled in is the one provider with no ceiling. That is
       exactly backwards, and it is silent.
    """
    if daily_budget_usd is None:
        return BudgetVerdict(allowed=True, detail="no daily budget configured")

    if cost_per_call_usd is None:
        return BudgetVerdict(
            allowed=False,
            detail=(
                f"daily_budget_usd is set ({daily_budget_usd}) but"
                " cost_per_call_usd is unknown — refusing to dispatch rather"
                " than spend against an unenforceable cap"
            ),
        )

    projected = spent_today_usd + cost_per_call_usd
    if projected > daily_budget_usd:
        return BudgetVerdict(
            allowed=False,
            detail=(
                f"spent {spent_today_usd} + {cost_per_call_usd} would reach"
                f" {projected}, over the {daily_budget_usd} daily budget"
            ),
        )
    return BudgetVerdict(
        allowed=True,
        detail=f"{daily_budget_usd - projected} headroom after this call",
    )


def headroom(
    *, daily_budget_usd: Decimal | None, spent_today_usd: Decimal
) -> Decimal | None:
    """Remaining budget, or None when no budget is configured.

    None rather than a large number: "unlimited" and "plenty left" are
    different facts and an alarm threshold must not treat them alike.
    """
    if daily_budget_usd is None:
        return None
    return max(daily_budget_usd - spent_today_usd, _ZERO)
