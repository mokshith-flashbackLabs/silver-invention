"""Per-provider, per-day metrics and the alarms computed from them.

The alarm that matters most here is the last one:

    any provider with zero successful calls in 24h

A provider silently returning zero results looks exactly like a quiet week for
infringements. In a safety product an undetected outage means users are told
they are clear when nothing actually looked — which is the failure this whole
subsystem exists to make impossible to miss.

Counts and cost come from ``provider_spend``, which is pre-aggregated. Latency
and success rate come from ``provider_calls`` over a **bounded** window
(``PROVIDER_ALARM_WINDOW_HOURS``, plus a fixed 24h window for the
zero-successful-calls check) using ``percentile_cont`` and filtered counts. There
is no unbounded aggregation anywhere on any path — this is an admin read, but a
query whose cost grows with total spend is a liability wherever it lives.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.providers.budget import headroom
from imageshield.providers.models import ProviderDailyStats, ProviderRuntime
from imageshield.providers.store import utc_spend_date
from imageshield.types import ProviderId

AlarmKind = Literal[
    "breaker_open",
    "daily_spend_near_budget",
    "monthly_spend_near_budget",
    "low_success_rate",
    "no_successful_calls_24h",
]

_ZERO = Decimal("0")

# Fixed, not configurable: the "is this provider alive at all" window. Making it
# a knob invites someone to widen it during an incident to quiet the alarm,
# which is the one alarm that must not be quietable.
_LIVENESS_WINDOW_HOURS = 24

# At most 31 pre-aggregated rows on the primary key, month-to-date, and the
# addition happens in Python. Not because 31 rows would be slow in Postgres, but
# because there is then no SQL aggregation over spend anywhere in this package —
# the rule that the dispatch path never aggregates holds by inspection of the
# whole directory rather than by remembering which module is which path.
#
# `monthly_budget_usd` is reported and alarmed on, NOT enforced at dispatch: the
# dispatch guard is deliberately one indexed row (providers/gate.py), and
# widening it to a month of rows would put a range scan on the request path.
_MONTH_SPEND_SQL = """
    SELECT spend_date, call_count, cost_usd
    FROM provider_spend
    WHERE provider_id = %(provider_id)s
      AND spend_date >= date_trunc('month', %(spend_date)s::date)::date
      AND spend_date <= %(spend_date)s
"""

# One pass over a bounded window. `attempt > 0` excludes the skip rows written
# by record_skip: a provider skipped by its own kill switch has no success rate,
# and counting skips as failures would alarm on a deliberate action.
_WINDOW_SQL = """
    SELECT
      count(*) FILTER (WHERE attempt > 0) AS window_calls,
      count(*) FILTER (WHERE attempt > 0 AND status = 'ok') AS window_ok,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE attempt > 0 AND latency_ms IS NOT NULL) AS p50,
      percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE attempt > 0 AND latency_ms IS NOT NULL) AS p99
    FROM provider_calls
    WHERE provider_id = %(provider_id)s
      AND created_at >= now() - make_interval(hours => %(window_hours)s)
"""

_LIVENESS_SQL = f"""
    SELECT count(*)
    FROM provider_calls
    WHERE provider_id = %(provider_id)s
      AND status = 'ok'
      AND created_at >= now() - interval '{_LIVENESS_WINDOW_HOURS} hours'
"""


class Alarm(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    kind: AlarmKind
    detail: str


class ProviderObservability(Protocol):
    async def daily_stats(
        self, runtime: ProviderRuntime, *, now: datetime, window_hours: int
    ) -> ProviderDailyStats: ...


class PostgresProviderObservability:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def daily_stats(
        self, runtime: ProviderRuntime, *, now: datetime, window_hours: int
    ) -> ProviderDailyStats:
        spend_date: date = utc_spend_date(now)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _MONTH_SPEND_SQL,
                {"provider_id": runtime.provider_id, "spend_date": spend_date},
            )
            month_rows = await cur.fetchall()
            cur = await conn.execute(
                _WINDOW_SQL,
                {"provider_id": runtime.provider_id, "window_hours": window_hours},
            )
            window = await cur.fetchone()
            cur = await conn.execute(
                _LIVENESS_SQL, {"provider_id": runtime.provider_id}
            )
            liveness = await cur.fetchone()

        today = next((r for r in month_rows if r[0] == spend_date), None)
        call_count = int(today[1]) if today is not None else 0
        cost_usd = Decimal(today[2]) if today is not None else _ZERO
        month_to_date = _ZERO
        for row in month_rows:
            month_to_date += Decimal(row[2])
        return ProviderDailyStats(
            provider_id=runtime.provider_id,
            enabled=runtime.enabled,
            breaker_state=runtime.breaker_state,
            breaker_reason=runtime.breaker_reason,
            call_count=call_count,
            cost_usd=cost_usd,
            daily_budget_usd=runtime.daily_budget_usd,
            monthly_budget_usd=runtime.monthly_budget_usd,
            month_to_date_cost_usd=month_to_date,
            budget_headroom_usd=headroom(
                daily_budget_usd=runtime.daily_budget_usd, spent_today_usd=cost_usd
            ),
            success_rate=_rate(window),
            window_call_count=int(window[0]) if window is not None else 0,
            successful_calls_24h=int(liveness[0]) if liveness is not None else 0,
            latency_p50_ms=_ms(window, 2),
            latency_p99_ms=_ms(window, 3),
        )


def _rate(window: tuple[Any, ...] | None) -> float | None:
    """Success rate over the window, or None when the window held no calls.

    None rather than 0.0: "no calls" and "every call failed" are different
    facts, and reporting the first as a 0% success rate would fire the
    low-success-rate alarm for every provider on a genuinely quiet hour.
    """
    if window is None or not window[0]:
        return None
    return int(window[1]) / int(window[0])


def _ms(window: tuple[Any, ...] | None, index: int) -> int | None:
    if window is None or window[index] is None:
        return None
    return int(window[index])


def alarms(
    stats: ProviderDailyStats,
    *,
    spend_alarm_fraction: float,
    success_rate_alarm: float,
) -> tuple[Alarm, ...]:
    """Every alarm currently firing for one provider.

    A disabled provider raises none of them: it was switched off deliberately,
    and alarming on a deliberate action trains people to ignore alarms. Its
    absence is still visible in ``enabled`` on the same payload.
    """
    if not stats.enabled:
        return ()

    found: list[Alarm] = []
    if stats.breaker_state != "closed":
        found.append(
            Alarm(
                provider_id=stats.provider_id,
                kind="breaker_open",
                detail=(
                    f"breaker {stats.breaker_state}:"
                    f" {stats.breaker_reason or 'no reason recorded'}"
                ),
            )
        )

    if stats.daily_budget_usd is not None and stats.daily_budget_usd > _ZERO:
        threshold = stats.daily_budget_usd * Decimal(str(spend_alarm_fraction))
        if stats.cost_usd >= threshold:
            found.append(
                Alarm(
                    provider_id=stats.provider_id,
                    kind="daily_spend_near_budget",
                    detail=(
                        f"spent {stats.cost_usd} of {stats.daily_budget_usd}"
                        f" ({spend_alarm_fraction:.0%} threshold)"
                    ),
                )
            )

    if stats.monthly_budget_usd is not None and stats.monthly_budget_usd > _ZERO:
        # The monthly budget's ONLY enforcement. It is deliberately not a
        # dispatch gate — that guard is one indexed row by design
        # (providers/gate.py) and a month is a range scan — so if this alarm did
        # not exist the column would be decoration, and a month-long overspend
        # would be visible only to somebody who went looking.
        threshold = stats.monthly_budget_usd * Decimal(str(spend_alarm_fraction))
        if stats.month_to_date_cost_usd >= threshold:
            found.append(
                Alarm(
                    provider_id=stats.provider_id,
                    kind="monthly_spend_near_budget",
                    detail=(
                        f"month-to-date {stats.month_to_date_cost_usd} of"
                        f" {stats.monthly_budget_usd}"
                        f" ({spend_alarm_fraction:.0%} threshold) — reported only,"
                        " never enforced at dispatch"
                    ),
                )
            )

    if stats.success_rate is not None and stats.success_rate < success_rate_alarm:
        found.append(
            Alarm(
                provider_id=stats.provider_id,
                kind="low_success_rate",
                detail=(
                    f"{stats.success_rate:.1%} over {stats.window_call_count} calls"
                    f" (threshold {success_rate_alarm:.0%})"
                ),
            )
        )

    if stats.successful_calls_24h == 0:
        # The one that matters most. Zero successful calls is indistinguishable
        # from a quiet week for infringements unless something says so out loud.
        found.append(
            Alarm(
                provider_id=stats.provider_id,
                kind="no_successful_calls_24h",
                detail=(
                    f"no successful call in {_LIVENESS_WINDOW_HOURS}h — an outage"
                    " here reads downstream as 'no matches found'"
                ),
            )
        )
    return tuple(found)
