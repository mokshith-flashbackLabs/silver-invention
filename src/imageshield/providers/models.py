"""Row and decision models for provider cost control.

Money is :class:`~decimal.Decimal` throughout and never a float. A per-call
cost of 0.003500 summed six million times in binary floating point drifts, and
the number this drifts away from is the one the budget guard compares against.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId

BreakerState = Literal["closed", "open", "half_open"]

# Why a provider was not called. Each is a `provider_calls.status` value, so a
# skip is visible in the data — a silent skip is indistinguishable from
# "nothing found", which is the distinction CLAUDE.md §7.5 exists to keep.
SkipReason = Literal["provider_disabled", "breaker_open", "budget_exceeded"]


class ProviderRuntime(BaseModel):
    """The dispatch-relevant state of one provider row.

    Read at most every ``PROVIDER_CONFIG_CACHE_SECONDS`` (capped at 30s), so a
    kill switch takes effect without a deploy and without a restart.
    """

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    enabled: bool
    cost_per_call_usd: Decimal | None
    daily_budget_usd: Decimal | None
    monthly_budget_usd: Decimal | None
    rate_limit_per_min: int | None
    breaker_state: BreakerState
    breaker_opened_at: datetime | None
    breaker_reason: str | None
    breaker_consecutive_failures: int
    breaker_cooldown_seconds: int | None


class DailySpend(BaseModel):
    """One pre-aggregated ``provider_spend`` row — the single indexed read the
    budget guard makes on the dispatch path."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    spend_date: date
    call_count: int
    cost_usd: Decimal


class Dispatch(BaseModel):
    """Cleared to call. ``cost_usd`` is what will be charged to
    ``provider_spend`` if the call is made — carried here so the recording path
    cannot disagree with the figure the guard checked against."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    cost_usd: Decimal | None
    # True when this is the ONE probe an open-then-cooled-down breaker allows.
    # Recorded so an operator reading provider_calls can see which call was the
    # recovery attempt rather than ordinary traffic.
    probe: bool = False


class Skip(BaseModel):
    """Refused before dispatch. Never fails the run (CLAUDE.md §7.6)."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    reason: SkipReason
    detail: str


Decision = Dispatch | Skip


class ProviderDailyStats(BaseModel):
    """One provider's day, for the observability surface.

    Assembled from ``provider_spend`` (the pre-aggregated counts and cost) plus
    a bounded recent window of ``provider_calls`` for latency and success rate.
    Never from a full-table aggregation.
    """

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    enabled: bool
    breaker_state: BreakerState
    breaker_reason: str | None
    call_count: int
    cost_usd: Decimal
    daily_budget_usd: Decimal | None
    monthly_budget_usd: Decimal | None
    month_to_date_cost_usd: Decimal
    # None when no budget is configured: "unlimited headroom" and "zero
    # headroom" must not render as the same number.
    budget_headroom_usd: Decimal | None
    # None when the window held no calls at all — which is itself the condition
    # the zero-successful-calls alarm fires on, and is not a 0% success rate.
    success_rate: float | None
    window_call_count: int
    successful_calls_24h: int
    latency_p50_ms: int | None
    latency_p99_ms: int | None
