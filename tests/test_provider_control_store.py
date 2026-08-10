"""PostgresProviderControlStore against real Postgres.

The done-when items that only Postgres can prove:

- the spend upsert and the ``provider_calls`` insert commit in ONE transaction,
  so a rolled-back call records no spend;
- ``provider_spend`` is read as one indexed row, never a ``SUM`` over
  ``provider_calls``;
- five consecutive timeouts open the breaker and a zero-match 200 does not;
- half-open allows exactly ONE probe, and the second concurrent claimant loses;
- a failed probe re-opens with a doubled, capped cooldown;
- the kill switch takes effect within the cache TTL with no deploy;
- admin writes land an ``audit_log`` row in the same transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.search.provider import ProviderResult
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import ensure_subject, run_migrate

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")
THRESHOLD = 5
COOLDOWN = 300
CAP = 1200
TODAY = date(2026, 8, 9)
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


@pytest.fixture
async def wired(
    migrated_db: str,
) -> AsyncIterator[tuple[PostgresProviderControlStore, UUID, str]]:
    """A control store, one real run to hang provider_calls off, and the db URL."""
    pool = make_async_pool(migrated_db, min_size=1, max_size=3)
    await pool.open()
    try:
        search = PostgresSearchStore(pool)
        user_ref = UserRef(uuid4())
        await ensure_subject(pool, user_ref)
        seed_id = await search.create_seed(user_ref, "user_supplied", "https://s3/x.jpg")
        run_id = await search.create_run(user_ref, seed_id, (HIVE, GOOGLE))
        yield (
            PostgresProviderControlStore(
                pool,
                cache_seconds=30.0,
                failure_threshold=THRESHOLD,
                default_cooldown_seconds=COOLDOWN,
                max_cooldown_seconds=CAP,
            ),
            run_id,
            migrated_db,
        )
    finally:
        await pool.close()


def _query(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _result(status: str, **kw: Any) -> ProviderResult:
    defaults: dict[str, Any] = {
        "provider_id": HIVE,
        "status": status,
        "matches": [],
        "raw_response": {"body": status},
        "http_status": 200 if status == "ok" else None,
        "latency_ms": 42,
    }
    return ProviderResult(**{**defaults, **kw})


# ── reads ────────────────────────────────────────────────────────────────


async def test_runtimes_reads_the_migration_seeded_cost_and_breaker_defaults(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, _, _ = wired
    runtimes = await control.runtimes()

    assert set(runtimes) == {HIVE, GOOGLE}
    # Migration 0009 fills Google's list price and deliberately leaves Hive's
    # contract price NULL rather than inventing one.
    assert runtimes[GOOGLE].cost_per_call_usd == Decimal("0.003500")
    assert runtimes[HIVE].cost_per_call_usd is None
    assert runtimes[HIVE].breaker_state == "closed"
    assert runtimes[HIVE].breaker_consecutive_failures == 0
    assert runtimes[HIVE].breaker_cooldown_seconds is None


async def test_daily_spend_returns_none_before_the_first_call(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, _, _ = wired
    assert await control.daily_spend(HIVE, TODAY) is None


# ── recording ────────────────────────────────────────────────────────────


async def test_call_row_and_spend_row_are_written_together(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired

    await control.record_outcome(
        run_id, _result("ok"), cost_usd=Decimal("0.0035"), spend_date=TODAY
    )
    await control.record_outcome(
        run_id, _result("ok"), cost_usd=Decimal("0.0035"), spend_date=TODAY
    )

    spend = await control.daily_spend(HIVE, TODAY)
    assert spend is not None
    assert spend.call_count == 2
    assert spend.cost_usd == Decimal("0.0070")
    assert _query(db, "SELECT count(*) FROM provider_calls WHERE status = 'ok'")[0][0] == 2


async def test_a_rolled_back_call_records_no_spend(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """The transaction boundary, proved by forcing a failure inside it.

    An unknown run_id violates provider_calls' FK, which aborts the transaction
    AFTER the lock and BEFORE the commit. Neither row may survive.
    """
    control, _, db = wired

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await control.record_outcome(
            uuid4(), _result("ok"), cost_usd=Decimal("5.00"), spend_date=TODAY
        )

    assert await control.daily_spend(HIVE, TODAY) is None
    assert _query(db, "SELECT count(*) FROM provider_calls")[0][0] == 0


async def test_a_failed_call_is_still_charged(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """Providers bill for served requests. Charging only successes would
    under-report exactly when a provider is misbehaving."""
    control, run_id, _ = wired

    await control.record_outcome(
        run_id, _result("timeout"), cost_usd=Decimal("0.0035"), spend_date=TODAY
    )

    spend = await control.daily_spend(HIVE, TODAY)
    assert spend is not None
    assert (spend.call_count, spend.cost_usd) == (1, Decimal("0.0035"))


async def test_an_unpriced_call_counts_but_costs_nothing(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """Hive's case until the contract figure lands: the call must never be
    invisible, only its price unknown."""
    control, run_id, _ = wired

    await control.record_outcome(run_id, _result("ok"), cost_usd=None, spend_date=TODAY)

    spend = await control.daily_spend(HIVE, TODAY)
    assert spend is not None
    assert (spend.call_count, spend.cost_usd) == (1, Decimal("0"))


async def test_a_sub_cent_price_accumulates_exactly_rather_than_rounding(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """provider_spend.cost_usd must carry at least the scale of the price that
    accumulates into it.

    The upsert coerces each increment to the column type BEFORE adding, so a
    narrower accumulator rounds every single call rather than rounding the total
    once. At the old NUMERIC(12,4), ten calls at 0.000040 stored 0.0000 — the
    accumulator never grew, the guard read spent=0 forever, and a configured
    daily budget silently stopped binding. That is a fail-OPEN in the one check
    INVARIANTS #38 requires to fail closed.

    Google's 0.003500 is exact at four decimals, which is why nothing would have
    shown today: this only surfaces on a contract price quoted per thousand calls
    with an odd third cent digit.
    """
    control, run_id, _ = wired
    price = Decimal("0.000040")

    for _ in range(10):
        await control.record_outcome(
            run_id, _result("ok"), cost_usd=price, spend_date=TODAY
        )

    spend = await control.daily_spend(HIVE, TODAY)
    assert spend is not None
    assert spend.call_count == 10
    assert spend.cost_usd == Decimal("0.000400")  # not 0.0000

    # A different provider, and a price whose per-call rounding would have biased
    # UPWARD: 0.00025 x 10 is 0.0025, where a 4-decimal accumulator produced
    # 0.0030 (+20%) and so tripped budget_exceeded early — skipping a provider
    # that still had headroom, which is avoidable partial coverage.
    for _ in range(10):
        await control.record_outcome(
            run_id,
            _result("ok", provider_id=GOOGLE),
            cost_usd=Decimal("0.000250"),
            spend_date=TODAY,
        )
    google_spend = await control.daily_spend(GOOGLE, TODAY)
    assert google_spend is not None
    assert google_spend.cost_usd == Decimal("0.002500")


async def test_raw_response_is_kept_verbatim_on_failure(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """Moved here from test_search_store.py with the write itself. raw_payload
    verbatim is the only way to recalibrate history (CLAUDE.md §7.2)."""
    control, run_id, db = wired
    body = {"why": "slow down", "nested": {"retry": 60}}

    await control.record_outcome(
        run_id,
        _result("rate_limited", raw_response=body, http_status=429, latency_ms=812, attempts=4),
        cost_usd=None,
        spend_date=TODAY,
    )

    rows = _query(
        db,
        "SELECT status, http_status, latency_ms, attempt, raw_response"
        " FROM provider_calls WHERE run_id = %s",
        (run_id,),
    )
    assert rows == [("rate_limited", 429, 812, 4, body)]


async def test_a_skip_writes_a_call_row_but_no_spend_and_no_breaker_change(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired

    await control.record_skip(run_id, HIVE, "budget_exceeded", "over the cap")

    rows = _query(
        db, "SELECT status, attempt, cost_usd, error_detail FROM provider_calls"
    )
    assert rows == [("budget_exceeded", 0, None, "over the cap")]
    assert await control.daily_spend(HIVE, TODAY) is None
    runtimes = await control.runtimes()
    assert runtimes[HIVE].breaker_consecutive_failures == 0
    assert runtimes[HIVE].breaker_state == "closed"


# ── breaker ──────────────────────────────────────────────────────────────


async def test_five_consecutive_timeouts_open_the_breaker(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, _ = wired

    for i in range(THRESHOLD):
        await control.record_outcome(
            run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
        )
        state = (await control.runtimes())[HIVE]
        expected = "open" if i == THRESHOLD - 1 else "closed"
        assert state.breaker_state == expected, f"after {i + 1} failures"

    opened = (await control.runtimes())[HIVE]
    assert opened.breaker_consecutive_failures == THRESHOLD
    assert opened.breaker_cooldown_seconds == COOLDOWN
    assert opened.breaker_opened_at is not None
    assert opened.breaker_reason == "timeout"


async def test_a_200_with_zero_matches_does_not_open_the_breaker(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """The most ordinary result this system produces. Opening on it would stop
    the scans that are supposed to notice a quiet week is not normal."""
    control, run_id, _ = wired

    for _ in range(THRESHOLD * 2):
        await control.record_outcome(
            run_id, _result("ok"), cost_usd=None, spend_date=TODAY
        )

    state = (await control.runtimes())[HIVE]
    assert state.breaker_state == "closed"
    assert state.breaker_consecutive_failures == 0


async def test_429_does_not_open_the_breaker(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, _ = wired

    for _ in range(THRESHOLD * 2):
        await control.record_outcome(
            run_id,
            _result("rate_limited", http_status=429),
            cost_usd=None,
            spend_date=TODAY,
        )

    state = (await control.runtimes())[HIVE]
    assert state.breaker_state == "closed"
    assert state.breaker_consecutive_failures == 0


async def test_one_success_resets_the_failure_count_mid_ladder(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, _ = wired

    for _ in range(THRESHOLD - 1):
        await control.record_outcome(
            run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
        )
    await control.record_outcome(run_id, _result("ok"), cost_usd=None, spend_date=TODAY)
    await control.record_outcome(
        run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
    )

    state = (await control.runtimes())[HIVE]
    assert state.breaker_state == "closed"
    assert state.breaker_consecutive_failures == 1


async def _open_breaker(
    control: PostgresProviderControlStore, run_id: UUID, db: str, *, cooled: bool
) -> None:
    for _ in range(THRESHOLD):
        await control.record_outcome(
            run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
        )
    if cooled:
        # Age the opened_at past the cooldown rather than sleeping 300s.
        _query(
            db,
            "UPDATE providers SET breaker_opened_at ="
            " now() - make_interval(secs => breaker_cooldown_seconds + 1)"
            " WHERE provider_id = %s RETURNING provider_id",
            (HIVE,),
        )
        control.invalidate()


async def test_half_open_allows_exactly_one_probe(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=True)

    assert await control.claim_half_open_probe(HIVE) is True
    # The second claimant loses: the conditional UPDATE no longer matches
    # because the state is already half_open. This is what makes "ONE probe"
    # true across N workers rather than one per process.
    assert await control.claim_half_open_probe(HIVE) is False


async def test_an_open_breaker_still_cooling_grants_no_probe(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=False)

    assert await control.claim_half_open_probe(HIVE) is False
    assert (await control.runtimes())[HIVE].breaker_state == "open"


async def test_an_inconclusive_probe_returns_to_open_and_can_be_probed_again(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """End-to-end proof that a 429 on the probe does not wedge the breaker.

    This is the whole-system version of the pure-state-machine test: claim the
    probe, answer it with `rate_limited` (which classifies as neutral), and check
    the row is back to 'open' — not stuck in 'half_open', which the claim query
    can never match again.
    """
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=True)
    assert await control.claim_half_open_probe(HIVE) is True

    await control.record_outcome(
        run_id, _result("rate_limited", http_status=429), cost_usd=None, spend_date=TODAY
    )

    reopened = (await control.runtimes())[HIVE]
    assert reopened.breaker_state == "open"
    assert reopened.breaker_cooldown_seconds == COOLDOWN  # restarted, not doubled

    # And crucially, the provider is probeable again once it cools. Before the
    # fix this returned False forever and only the admin reset could recover it.
    _query(
        db,
        "UPDATE providers SET breaker_opened_at ="
        " now() - make_interval(secs => breaker_cooldown_seconds + 1)"
        " WHERE provider_id = %s RETURNING provider_id",
        (HIVE,),
    )
    control.invalidate()
    assert await control.claim_half_open_probe(HIVE) is True


async def test_a_probe_abandoned_by_a_dead_worker_is_reclaimed(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """A worker can claim the probe and then die — SIGKILL, eviction, a crash
    before record_outcome. Nothing would then move the row out of 'half_open':
    the claim's first disjunct needs 'open' and record_outcome is never reached.

    The reclaim waits a whole extra cooldown beyond the one that authorised the
    probe, so it cannot steal a probe that is merely slow.
    """
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=True)
    assert await control.claim_half_open_probe(HIVE) is True  # then "die"

    # Still inside the grace period: the probe might be in flight, hands off.
    assert await control.claim_half_open_probe(HIVE) is False

    _query(
        db,
        "UPDATE providers SET breaker_opened_at ="
        " now() - make_interval(secs => 2 * breaker_cooldown_seconds + 1)"
        " WHERE provider_id = %s RETURNING provider_id",
        (HIVE,),
    )
    control.invalidate()
    assert await control.claim_half_open_probe(HIVE) is True


async def test_a_straggler_failure_on_an_open_breaker_does_not_escalate(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """Reachable via the runtime cache: another worker's in-flight call lands
    after the breaker opened. It must be counted but must not double the cooldown
    or restart the clock — otherwise one outage with N workers races the cooldown
    to its cap and delays recovery for reasons unrelated to the provider."""
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=False)
    before = (await control.runtimes())[HIVE]
    opened_at_before = _query(
        db, "SELECT breaker_opened_at FROM providers WHERE provider_id = %s", (HIVE,)
    )[0][0]

    await control.record_outcome(
        run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
    )

    after = (await control.runtimes())[HIVE]
    assert after.breaker_state == "open"
    assert after.breaker_consecutive_failures == before.breaker_consecutive_failures + 1
    assert after.breaker_cooldown_seconds == before.breaker_cooldown_seconds
    assert after.breaker_opened_at == opened_at_before  # clock untouched


async def test_a_counter_bump_never_clears_the_cooldown_clock(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    """The tri-state `opened_at` at the SQL level. When it was a boolean the
    UPDATE read it as "now() or else NULL", so the sub-threshold failure that
    merely increments the counter wiped breaker_opened_at — the column the
    half-open claim measures its cooldown from."""
    control, run_id, db = wired
    for _ in range(THRESHOLD):
        await control.record_outcome(
            run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
        )
    opened_at = _query(
        db, "SELECT breaker_opened_at FROM providers WHERE provider_id = %s", (HIVE,)
    )[0][0]
    assert opened_at is not None

    # One more failure: a straggler. The clock must survive it.
    await control.record_outcome(
        run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
    )
    assert (
        _query(
            db, "SELECT breaker_opened_at FROM providers WHERE provider_id = %s", (HIVE,)
        )[0][0]
        == opened_at
    )


async def test_a_failed_probe_reopens_with_a_doubled_capped_cooldown(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=True)
    assert await control.claim_half_open_probe(HIVE) is True

    await control.record_outcome(
        run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
    )
    doubled = (await control.runtimes())[HIVE]
    assert doubled.breaker_state == "open"
    assert doubled.breaker_cooldown_seconds == 2 * COOLDOWN

    # Again, and again, until the cap holds it.
    for expected in (4 * COOLDOWN, CAP, CAP):
        _query(
            db,
            "UPDATE providers SET breaker_opened_at ="
            " now() - make_interval(secs => breaker_cooldown_seconds + 1)"
            " WHERE provider_id = %s RETURNING provider_id",
            (HIVE,),
        )
        control.invalidate()
        assert await control.claim_half_open_probe(HIVE) is True
        await control.record_outcome(
            run_id, _result("timeout"), cost_usd=None, spend_date=TODAY
        )
        assert (await control.runtimes())[HIVE].breaker_cooldown_seconds == expected


async def test_a_successful_probe_closes_the_breaker_and_clears_the_ladder(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=True)
    assert await control.claim_half_open_probe(HIVE) is True

    await control.record_outcome(run_id, _result("ok"), cost_usd=None, spend_date=TODAY)

    recovered = (await control.runtimes())[HIVE]
    assert recovered.breaker_state == "closed"
    assert recovered.breaker_consecutive_failures == 0
    assert recovered.breaker_cooldown_seconds is None
    assert recovered.breaker_opened_at is None
    assert recovered.breaker_reason is None


async def test_the_breaker_is_per_provider(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=False)

    runtimes = await control.runtimes()
    assert runtimes[HIVE].breaker_state == "open"
    assert runtimes[GOOGLE].breaker_state == "closed"


# ── kill switch + audit ──────────────────────────────────────────────────


async def test_disable_writes_the_flag_and_exactly_one_audit_row(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, _, db = wired

    assert await control.set_enabled(
        HIVE, False, actor="ops", reason="billing surprise"
    )

    assert (await control.runtimes())[HIVE].enabled is False
    rows = _query(db, "SELECT actor_type, action, metadata FROM audit_log")
    assert len(rows) == 1
    actor_type, action, metadata = rows[0]
    assert (actor_type, action) == ("operator", "provider.disabled")
    assert metadata["reason"] == "billing surprise"
    assert metadata["provider_id"] == HIVE


async def test_enable_and_reset_are_separate_actions_with_separate_audit_rows(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, db = wired
    await _open_breaker(control, run_id, db, cooled=False)

    assert await control.set_enabled(HIVE, True, actor="ops", reason="vendor fixed it")
    assert await control.reset_breaker(HIVE, actor="ops", reason="verified by hand")

    state = (await control.runtimes())[HIVE]
    assert state.breaker_state == "closed"
    assert state.breaker_consecutive_failures == 0
    assert state.breaker_cooldown_seconds is None
    actions = [row[0] for row in _query(db, "SELECT action FROM audit_log ORDER BY audit_id")]
    assert actions == ["provider.enabled", "provider.breaker_reset"]


async def test_an_unknown_provider_writes_nothing_and_reports_failure(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, _, db = wired
    ghost = ProviderId("pimeyes")

    assert await control.set_enabled(ghost, False, actor="ops", reason="x") is False
    assert await control.reset_breaker(ghost, actor="ops", reason="x") is False

    assert _query(db, "SELECT count(*) FROM audit_log")[0][0] == 0


async def test_recording_an_outcome_for_an_unknown_provider_raises(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, run_id, _ = wired
    with pytest.raises(LookupError):
        await control.record_outcome(
            run_id,
            _result("ok", provider_id=ProviderId("pimeyes")),
            cost_usd=None,
            spend_date=TODAY,
        )


# ── cache ────────────────────────────────────────────────────────────────


async def test_the_runtime_cache_expires_so_a_kill_switch_bites_without_a_deploy(
    migrated_db: str,
) -> None:
    """An out-of-process change (another pod's admin call, or psql) must reach
    this process within the TTL. The clock is injected rather than slept."""
    # One tick per runtimes() call: populate, inside the TTL, past it.
    ticks = iter([0.0, 5.0, 31.0])
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        control = PostgresProviderControlStore(
            pool,
            cache_seconds=30.0,
            failure_threshold=THRESHOLD,
            default_cooldown_seconds=COOLDOWN,
            max_cooldown_seconds=CAP,
            clock=lambda: next(ticks),
        )
        assert (await control.runtimes())[HIVE].enabled is True

        _query(
            migrated_db,
            "UPDATE providers SET enabled = false WHERE provider_id = %s"
            " RETURNING provider_id",
            (HIVE,),
        )

        # t=5s: still inside the TTL, still the cached value.
        assert (await control.runtimes())[HIVE].enabled is True
        # t=31s: past it, so the change is seen with no deploy and no restart.
        assert (await control.runtimes())[HIVE].enabled is False
    finally:
        await pool.close()


async def test_this_processes_own_admin_write_invalidates_immediately(
    wired: tuple[PostgresProviderControlStore, UUID, str],
) -> None:
    control, _, _ = wired
    assert (await control.runtimes())[HIVE].enabled is True  # populate the cache

    await control.set_enabled(HIVE, False, actor="ops", reason="incident")

    # No TTL wait: an operator who disables a provider and re-reads the admin
    # surface must not be shown their own stale cache entry.
    assert (await control.runtimes())[HIVE].enabled is False


# ── the request path never aggregates ────────────────────────────────────


def test_no_sql_aggregation_over_spend_anywhere_in_the_providers_package() -> None:
    """Step-8 done-when, as a tripwire rather than a one-off grep.

    provider_calls grows with every call ever made. A budget guard built on
    SUM() over it would get slower in proportion to how much has been spent —
    the cost check would itself become a cost. The whole package is scanned, not
    just the dispatch path, so the rule holds by inspection of the directory
    rather than by remembering which module is on which path.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "imageshield" / "providers"
    files = sorted(package.rglob("*.py"))
    assert files, "providers/ scan found nothing — path wrong?"
    offenders = [
        f"{path.name}:{i}: {line.strip()}"
        for path in files
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "SUM(" in line.upper()
    ]
    assert offenders == []
