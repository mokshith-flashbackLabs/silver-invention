"""The per-provider daily rollup against real Postgres.

Two things only Postgres can settle: that the latency percentiles and success
rate come off a bounded window of real ``provider_calls`` rows, and that skip
rows (``attempt = 0``) are excluded from the success rate — counting a
deliberate kill switch as a failure would alarm on an action somebody took on
purpose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.providers.observability import PostgresProviderObservability, alarms
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.search.provider import ProviderResult
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import ensure_subject, run_migrate

HIVE = ProviderId("hive")
NOW = datetime.now(UTC)
TODAY: date = NOW.date()

Wired = tuple[
    PostgresProviderControlStore, PostgresProviderObservability, UUID, str
]


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


@pytest.fixture
async def wired(migrated_db: str) -> AsyncIterator[Wired]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=3)
    await pool.open()
    try:
        search = PostgresSearchStore(pool)
        user_ref = UserRef(uuid4())
        await ensure_subject(pool, user_ref)
        seed_id = await search.create_seed(user_ref, "user_supplied", "https://s3/x.jpg")
        run_id = await search.create_run(
            user_ref, seed_id, (HIVE,), seed_url="https://proxy-s3.example/run.jpg?X-Amz-Signature=fixture"
        )
        control = PostgresProviderControlStore(
            pool,
            cache_seconds=0.0,
            failure_threshold=100,  # high: these tests are not about the breaker
            default_cooldown_seconds=300,
            max_cooldown_seconds=1200,
        )
        yield control, PostgresProviderObservability(pool), run_id, migrated_db
    finally:
        await pool.close()


def _result(status: str, latency_ms: int) -> ProviderResult:
    return ProviderResult(
        provider_id=HIVE,
        status=status,  # type: ignore[arg-type]
        matches=[],
        raw_response={},
        http_status=200 if status == "ok" else None,
        latency_ms=latency_ms,
    )


async def test_an_untouched_provider_reports_zeros_and_a_null_success_rate(
    wired: Wired,
) -> None:
    control, observability, _, _ = wired
    hive = (await control.runtimes())[HIVE]

    stats = await observability.daily_stats(hive, now=NOW, window_hours=1)

    assert stats.call_count == 0
    assert stats.cost_usd == Decimal("0")
    assert stats.month_to_date_cost_usd == Decimal("0")
    # None, not 0.0: "no calls" and "every call failed" are different facts, and
    # reporting the first as a 0% rate would alarm on a genuinely quiet hour.
    assert stats.success_rate is None
    assert stats.latency_p50_ms is None
    assert stats.successful_calls_24h == 0


async def test_counts_cost_headroom_success_rate_and_percentiles(
    wired: Wired, migrated_db: str
) -> None:
    control, observability, run_id, db = wired
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "UPDATE providers SET cost_per_call_usd = 1.00, daily_budget_usd = 10.00,"
            " monthly_budget_usd = 100.00 WHERE provider_id = %s",
            (HIVE,),
        )
    control.invalidate()

    for latency in (100, 200, 300):
        await control.record_outcome(
            run_id, _result("ok", latency), cost_usd=Decimal("1.00"), spend_date=TODAY
        )
    await control.record_outcome(
        run_id, _result("timeout", 5000), cost_usd=Decimal("1.00"), spend_date=TODAY
    )

    hive = (await control.runtimes())[HIVE]
    stats = await observability.daily_stats(hive, now=NOW, window_hours=1)

    assert stats.call_count == 4
    assert stats.cost_usd == Decimal("4.0000")
    assert stats.month_to_date_cost_usd == Decimal("4.0000")
    assert stats.budget_headroom_usd == Decimal("6.0000")
    assert stats.monthly_budget_usd == Decimal("100.00")
    assert stats.window_call_count == 4
    assert stats.success_rate == 0.75
    assert stats.successful_calls_24h == 3
    assert stats.latency_p50_ms == 250  # median of 100/200/300/5000
    assert stats.latency_p99_ms is not None and stats.latency_p99_ms > 250


async def test_skip_rows_are_excluded_from_the_success_rate(wired: Wired) -> None:
    control, observability, run_id, _ = wired

    await control.record_outcome(run_id, _result("ok", 100), cost_usd=None, spend_date=TODAY)
    for reason in ("provider_disabled", "breaker_open", "budget_exceeded"):
        await control.record_skip(run_id, HIVE, reason, "skipped")  # type: ignore[arg-type]

    hive = (await control.runtimes())[HIVE]
    stats = await observability.daily_stats(hive, now=NOW, window_hours=1)

    # One real call, one success. The three skips are visible as provider_calls
    # rows but are not evidence about the provider's health.
    assert stats.window_call_count == 1
    assert stats.success_rate == 1.0


async def test_the_window_is_bounded(wired: Wired, migrated_db: str) -> None:
    control, observability, run_id, db = wired
    await control.record_outcome(run_id, _result("ok", 100), cost_usd=None, spend_date=TODAY)
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("UPDATE provider_calls SET created_at = now() - interval '3 hours'")

    hive = (await control.runtimes())[HIVE]
    one_hour = await observability.daily_stats(hive, now=NOW, window_hours=1)
    six_hours = await observability.daily_stats(hive, now=NOW, window_hours=6)

    assert one_hour.window_call_count == 0
    assert one_hour.success_rate is None
    assert six_hours.window_call_count == 1
    # The 24h liveness window is FIXED, not the configurable one — an alarm you
    # can widen during an incident is an alarm you can silence.
    assert one_hour.successful_calls_24h == 1


async def test_month_to_date_spans_prior_days_but_not_the_prior_month(
    wired: Wired, migrated_db: str
) -> None:
    control, observability, _, db = wired
    first_of_month = TODAY.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    rows = [(HIVE, last_month, Decimal("50.00")), (HIVE, TODAY, Decimal("3.00"))]
    if first_of_month != TODAY:
        rows.insert(1, (HIVE, first_of_month, Decimal("2.00")))
    with psycopg.connect(db, autocommit=True) as conn:
        conn.cursor().executemany(
            "INSERT INTO provider_spend (provider_id, spend_date, call_count, cost_usd)"
            " VALUES (%s, %s, 1, %s)",
            rows,
        )

    hive = (await control.runtimes())[HIVE]
    stats = await observability.daily_stats(hive, now=NOW, window_hours=1)

    expected_month = Decimal("5.0000") if first_of_month != TODAY else Decimal("3.0000")
    assert stats.cost_usd == Decimal("3.0000")  # today only
    assert stats.month_to_date_cost_usd == expected_month  # last month excluded


# ── alarm arithmetic (pure) ──────────────────────────────────────────────


def _stats_for_alarms(**overrides: Any) -> Any:
    from imageshield.providers.models import ProviderDailyStats

    defaults: dict[str, Any] = {
        "provider_id": HIVE,
        "enabled": True,
        "breaker_state": "closed",
        "breaker_reason": None,
        "call_count": 10,
        "cost_usd": Decimal("1.00"),
        "daily_budget_usd": Decimal("10.00"),
        "monthly_budget_usd": None,
        "month_to_date_cost_usd": Decimal("1.00"),
        "budget_headroom_usd": Decimal("9.00"),
        "success_rate": 1.0,
        "window_call_count": 10,
        "successful_calls_24h": 10,
        "latency_p50_ms": 100,
        "latency_p99_ms": 200,
    }
    return ProviderDailyStats(**{**defaults, **overrides})


def _kinds(**overrides: Any) -> set[str]:
    return {
        a.kind
        for a in alarms(
            _stats_for_alarms(**overrides),
            spend_alarm_fraction=0.8,
            success_rate_alarm=0.9,
        )
    }


def test_the_spend_alarm_fires_at_the_threshold_not_past_it() -> None:
    assert "daily_spend_near_budget" not in _kinds(cost_usd=Decimal("7.99"))
    assert "daily_spend_near_budget" in _kinds(cost_usd=Decimal("8.00"))


def test_a_provider_with_no_budget_never_raises_the_spend_alarm() -> None:
    assert _kinds(daily_budget_usd=None, cost_usd=Decimal("999")) == set()


def test_the_monthly_budget_is_actually_alarmed_on() -> None:
    """SCHEMA.md and providers/observability.py both say monthly_budget_usd is
    "reported and alarmed on, not enforced at dispatch". The second clause was
    false — AlarmKind had no monthly member — which made the column decoration: a
    month-long overspend would have been visible only to somebody who went
    looking at the right screen.

    It stays out of the dispatch guard on purpose (that check is one indexed row
    by design; a month is a range scan), so this alarm is its ONLY enforcement.
    """
    assert "monthly_spend_near_budget" not in _kinds(
        monthly_budget_usd=Decimal("100.00"), month_to_date_cost_usd=Decimal("79.99")
    )
    firing = _kinds(
        monthly_budget_usd=Decimal("100.00"), month_to_date_cost_usd=Decimal("80.00")
    )
    assert "monthly_spend_near_budget" in firing
    # And it is independent of the daily one: a slow overspend across a month can
    # sit under the daily cap every single day.
    assert "daily_spend_near_budget" not in firing


def test_a_provider_with_no_monthly_budget_raises_no_monthly_alarm() -> None:
    assert "monthly_spend_near_budget" not in _kinds(
        monthly_budget_usd=None, month_to_date_cost_usd=Decimal("9999")
    )


def test_a_quiet_hour_does_not_look_like_a_broken_provider() -> None:
    """success_rate None (no calls in the window) must not fire the low-rate
    alarm. The zero-successful-calls alarm is the one that catches a real
    outage, and it uses the fixed 24h window."""
    assert _kinds(success_rate=None, window_call_count=0) == set()
    assert _kinds(success_rate=None, window_call_count=0, successful_calls_24h=0) == {
        "no_successful_calls_24h"
    }


def test_zero_successful_calls_in_24h_is_always_reported() -> None:
    """The alarm that matters most: a provider silently returning nothing looks
    exactly like a quiet week for infringements."""
    firing = alarms(
        _stats_for_alarms(successful_calls_24h=0),
        spend_alarm_fraction=0.8,
        success_rate_alarm=0.9,
    )
    [alarm] = [a for a in firing if a.kind == "no_successful_calls_24h"]
    assert "no matches found" in alarm.detail
