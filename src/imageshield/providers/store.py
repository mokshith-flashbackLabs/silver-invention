"""Provider control-plane persistence — raw SQL, no ORM (CLAUDE.md §2).

Three responsibilities, and the reason they share a module is that all three
have to agree about the same provider row:

1. **Reads for the dispatch guard**, with a TTL-capped cache so a kill switch
   takes effect within seconds and a run does not pay a round trip per provider.
2. **Recording an outcome**: the ``provider_calls`` insert, the
   ``provider_spend`` upsert and the breaker transition, in ONE transaction. A
   rolled-back call must record no spend, and a spend recorded without its call
   is money with no provenance.
3. **Admin writes** — the kill switch and the breaker reset — each paired with
   its ``audit_log`` row in one transaction.

There is deliberately no ``SUM`` over ``provider_calls`` anywhere in this
package. Today's spend is one indexed row in ``provider_spend``; the guard that
exists to protect spend must not get slower as spend accumulates.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.providers.breaker import BreakerTransition, classify, transition
from imageshield.providers.models import DailySpend, ProviderRuntime, SkipReason
from imageshield.search.provider import ProviderResult
from imageshield.types import ProviderId, parse_provider_id

log = structlog.get_logger("imageshield.providers")

_RUNTIME_COLUMNS = (
    "provider_id, enabled, cost_per_call_usd, daily_budget_usd, monthly_budget_usd,"
    " rate_limit_per_min, breaker_state, breaker_opened_at, breaker_reason,"
    " breaker_consecutive_failures, breaker_cooldown_seconds"
)

_RUNTIMES_SQL = f"SELECT {_RUNTIME_COLUMNS} FROM providers ORDER BY provider_id"

# FOR UPDATE: the breaker's failure counter is read-modify-write, and without
# the lock N concurrent workers each read the same count and each write count+1,
# so a provider needs N x threshold failures to open instead of threshold.
_LOCK_RUNTIME_SQL = f"""
    SELECT {_RUNTIME_COLUMNS} FROM providers
    WHERE provider_id = %(provider_id)s
    FOR UPDATE
"""

# One indexed row. The PRIMARY KEY (provider_id, spend_date) is the index.
_SPEND_SQL = """
    SELECT provider_id, spend_date, call_count, cost_usd
    FROM provider_spend
    WHERE provider_id = %(provider_id)s AND spend_date = %(spend_date)s
"""

_UPSERT_SPEND_SQL = """
    INSERT INTO provider_spend (provider_id, spend_date, call_count, cost_usd)
    VALUES (%(provider_id)s, %(spend_date)s, 1, %(cost_usd)s)
    ON CONFLICT (provider_id, spend_date) DO UPDATE
      SET call_count = provider_spend.call_count + 1,
          cost_usd = provider_spend.cost_usd + EXCLUDED.cost_usd,
          updated_at = now()
"""

_INSERT_CALL_SQL = """
    INSERT INTO provider_calls (run_id, provider_id, status, http_status,
                                latency_ms, cost_usd, attempt, error_detail,
                                raw_response)
    VALUES (%(run_id)s, %(provider_id)s, %(status)s, %(http_status)s,
            %(latency_ms)s, %(cost_usd)s, %(attempt)s, %(error_detail)s,
            %(raw_response)s)
"""

_APPLY_BREAKER_SQL = """
    UPDATE providers
    SET breaker_state = %(state)s,
        breaker_consecutive_failures = %(consecutive_failures)s,
        breaker_cooldown_seconds = %(cooldown_seconds)s,
        breaker_reason = %(reason)s,
        breaker_opened_at = CASE %(opened_at)s
                              WHEN 'now'   THEN now()
                              WHEN 'clear' THEN NULL
                              ELSE providers.breaker_opened_at
                            END
    WHERE provider_id = %(provider_id)s
"""

# The half-open claim. Exactly one caller can win this UPDATE: a losing
# concurrent worker matches zero rows and keeps skipping. This is why "allow ONE
# probe" is enforced by the database and not by a per-process flag.
#
# COALESCE(breaker_cooldown_seconds, default): an 'open' row written before this
# column existed, or by a hand-edit, still gets a defined cooldown.
#
# The second disjunct is the stale-probe reclaim, and without it the breaker has
# a terminal state. A worker that claims the probe and then dies — SIGKILL, pod
# eviction, a crash between the claim and record_outcome — leaves the row in
# 'half_open' with nothing able to move it: this query's first disjunct requires
# 'open', and record_outcome is never reached. The provider would be skipped on
# every subsequent run until a human ran the admin reset, which in a safety
# product means permanent partial coverage waiting on somebody noticing.
#
# The grace period is what stops the reclaim from stealing a probe that is merely
# slow: a live probe can legitimately take up to PROVIDER_TIMEOUT_SECONDS plus
# its bounded 429 retries, so the reclaim waits a whole extra cooldown beyond the
# one that authorised the probe in the first place.
_CLAIM_HALF_OPEN_SQL = """
    UPDATE providers
    SET breaker_state = 'half_open'
    WHERE provider_id = %(provider_id)s
      AND breaker_opened_at IS NOT NULL
      AND (
        (breaker_state = 'open'
         AND breaker_opened_at + make_interval(
               secs => COALESCE(breaker_cooldown_seconds, %(default_cooldown)s)
             ) <= now())
        OR
        (breaker_state = 'half_open'
         AND breaker_opened_at + make_interval(
               secs => COALESCE(breaker_cooldown_seconds, %(default_cooldown)s)
                       + %(stale_probe_grace)s
             ) <= now())
      )
    RETURNING provider_id
"""

_SET_ENABLED_SQL = """
    UPDATE providers SET enabled = %(enabled)s
    WHERE provider_id = %(provider_id)s
    RETURNING provider_id
"""

_RESET_BREAKER_SQL = """
    UPDATE providers
    SET breaker_state = 'closed',
        breaker_consecutive_failures = 0,
        breaker_cooldown_seconds = NULL,
        breaker_reason = NULL,
        breaker_opened_at = NULL
    WHERE provider_id = %(provider_id)s
    RETURNING provider_id
"""

# actor_type 'operator': these are console actions taken during an incident,
# the same category as the calibration CLI's writes.
_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES ('operator', %(action)s, NULL, %(metadata)s)
"""

PROVIDER_DISABLED_ACTION = "provider.disabled"
PROVIDER_ENABLED_ACTION = "provider.enabled"
BREAKER_RESET_ACTION = "provider.breaker_reset"


def _to_runtime(row: tuple[Any, ...]) -> ProviderRuntime:
    return ProviderRuntime(
        provider_id=parse_provider_id(row[0]),
        enabled=row[1],
        cost_per_call_usd=row[2],
        daily_budget_usd=row[3],
        monthly_budget_usd=row[4],
        rate_limit_per_min=row[5],
        breaker_state=row[6],
        breaker_opened_at=row[7],
        breaker_reason=row[8],
        breaker_consecutive_failures=row[9],
        breaker_cooldown_seconds=row[10],
    )


class ProviderControlStore(Protocol):
    async def runtimes(self) -> Mapping[ProviderId, ProviderRuntime]: ...

    async def daily_spend(
        self, provider_id: ProviderId, spend_date: date
    ) -> DailySpend | None: ...

    async def claim_half_open_probe(self, provider_id: ProviderId) -> bool: ...

    async def record_outcome(
        self,
        run_id: UUID,
        result: ProviderResult,
        *,
        cost_usd: Decimal | None,
        spend_date: date,
        probe: bool = False,
    ) -> None: ...

    async def record_skip(
        self, run_id: UUID, provider_id: ProviderId, reason: SkipReason, detail: str
    ) -> None: ...

    async def set_enabled(
        self, provider_id: ProviderId, enabled: bool, *, actor: str, reason: str
    ) -> bool: ...

    async def reset_breaker(
        self, provider_id: ProviderId, *, actor: str, reason: str
    ) -> bool: ...


class PostgresProviderControlStore:
    """Pool-backed control store with a TTL-capped runtime cache.

    The cache is per-instance, which means per-process: the HTTP app and each
    worker hold their own. That is correct for a kill switch — every process
    re-reads within the TTL independently, so none of them can be stuck on a
    stale value waiting for another to refresh.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        cache_seconds: float,
        failure_threshold: int,
        default_cooldown_seconds: int,
        max_cooldown_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._cache_seconds = cache_seconds
        self._failure_threshold = failure_threshold
        self._default_cooldown_seconds = default_cooldown_seconds
        self._max_cooldown_seconds = max_cooldown_seconds
        self._clock = clock
        self._cached: Mapping[ProviderId, ProviderRuntime] | None = None
        self._cached_at: float | None = None

    # ── reads ────────────────────────────────────────────────────────────

    async def runtimes(self) -> Mapping[ProviderId, ProviderRuntime]:
        now = self._clock()
        if (
            self._cached is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_seconds
        ):
            return self._cached
        async with self._pool.connection() as conn:
            cur = await conn.execute(_RUNTIMES_SQL)
            rows = await cur.fetchall()
        loaded = {parse_provider_id(row[0]): _to_runtime(row) for row in rows}
        self._cached = loaded
        self._cached_at = now
        return loaded

    def invalidate(self) -> None:
        """Drop the cache so the next read hits Postgres.

        Called by this process's own admin writes. It does not help other
        processes — their TTL does — but it means an operator who disables a
        provider and immediately re-reads the admin surface sees their own
        change rather than a stale cache entry.
        """
        self._cached = None
        self._cached_at = None

    async def daily_spend(
        self, provider_id: ProviderId, spend_date: date
    ) -> DailySpend | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _SPEND_SQL, {"provider_id": provider_id, "spend_date": spend_date}
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return DailySpend(
            provider_id=parse_provider_id(row[0]),
            spend_date=row[1],
            call_count=row[2],
            cost_usd=row[3],
        )

    # ── breaker ──────────────────────────────────────────────────────────

    async def claim_half_open_probe(self, provider_id: ProviderId) -> bool:
        """Atomically take the single probe an open, cooled-down breaker allows.

        Returns True to exactly one caller. Everyone else gets False and keeps
        skipping, which is what "allow ONE probe" has to mean when more than one
        worker is running.

        Also reclaims a probe abandoned by a worker that died mid-flight — see
        the second disjunct in ``_CLAIM_HALF_OPEN_SQL``. Without that, a crash
        between claiming and recording leaves the breaker stuck in ``half_open``
        with no code path out.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _CLAIM_HALF_OPEN_SQL,
                {
                    "provider_id": provider_id,
                    "default_cooldown": self._default_cooldown_seconds,
                    "stale_probe_grace": self._default_cooldown_seconds,
                },
            )
            claimed = await cur.fetchone()
        if claimed is not None:
            self.invalidate()
            log.info("provider.breaker_half_open", provider_id=provider_id)
        return claimed is not None

    # ── writes ───────────────────────────────────────────────────────────

    async def record_outcome(
        self,
        run_id: UUID,
        result: ProviderResult,
        *,
        cost_usd: Decimal | None,
        spend_date: date,
        probe: bool = False,
    ) -> None:
        """Record a call that was actually made: the ``provider_calls`` row, the
        ``provider_spend`` upsert, and the breaker transition — one transaction.

        ``probe`` is for the log line only. The breaker transition deliberately
        does **not** read it: it keys off the LOCKED row's ``breaker_state``,
        which is authoritative and already says ``half_open`` for a probe,
        whereas this flag was derived from a cached snapshot the guard read
        earlier. Two sources of truth for "was this the probe" is one too many,
        and the row is the one that cannot be stale.

        Spend is charged whenever a request left this process, including
        failures: providers bill for served requests, and a timeout that the
        provider counted is money gone. Charging only successes would
        under-report exactly when a provider is misbehaving, which is when the
        number matters most.

        ``cost_usd`` may be None (price unknown). The ``provider_calls`` row
        still gets written with a NULL cost and ``provider_spend.call_count``
        still increments, so the call is never invisible — only its price is.
        """
        outcome = classify(result.status)
        change: BreakerTransition | None = None
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _LOCK_RUNTIME_SQL, {"provider_id": result.provider_id}
            )
            locked = await cur.fetchone()
            if locked is None:
                raise LookupError(f"unknown provider {result.provider_id!r}")
            runtime = _to_runtime(locked)

            await conn.execute(
                _INSERT_CALL_SQL,
                {
                    "run_id": run_id,
                    "provider_id": result.provider_id,
                    "status": result.status,
                    "http_status": result.http_status,
                    "latency_ms": result.latency_ms,
                    "cost_usd": cost_usd,
                    "attempt": result.attempts,
                    "error_detail": result.error_detail,
                    "raw_response": Jsonb(result.raw_response),
                },
            )
            await conn.execute(
                _UPSERT_SPEND_SQL,
                {
                    "provider_id": result.provider_id,
                    "spend_date": spend_date,
                    "cost_usd": cost_usd if cost_usd is not None else Decimal("0"),
                },
            )
            change = transition(
                # The LOCKED row's state, not the cached snapshot the guard saw:
                # between the guard's read and this lock another worker may have
                # opened the breaker, and the transition has to reason about what
                # is actually stored.
                state=runtime.breaker_state,
                consecutive_failures=runtime.breaker_consecutive_failures,
                cooldown_seconds=runtime.breaker_cooldown_seconds,
                outcome=outcome,
                failure_threshold=self._failure_threshold,
                default_cooldown_seconds=self._default_cooldown_seconds,
                max_cooldown_seconds=self._max_cooldown_seconds,
                reason=_breaker_reason(result),
            )
            if change.changed:
                await self._apply_breaker(conn, result.provider_id, change)
        # Only reached on commit; a rolled-back transaction leaves the cache
        # alone and raises, so no half-applied state is ever announced.
        if change.changed:
            self.invalidate()
        if probe or runtime.breaker_state == "half_open":
            log.warning(
                "provider.breaker_probe_result",
                provider_id=result.provider_id,
                status=result.status,
                outcome=outcome,
                breaker_state=change.state,
            )
        if change.opened:
            # The alarm. Logged at error level because a provider being taken
            # out of rotation means partial coverage on every run until it comes
            # back, and partial coverage in a safety product means users are
            # told they are clear when something did not look.
            log.error(
                "provider.breaker_opened",
                provider_id=result.provider_id,
                consecutive_failures=change.consecutive_failures,
                cooldown_seconds=change.cooldown_seconds,
                reason=change.reason,
            )

    async def record_skip(
        self, run_id: UUID, provider_id: ProviderId, reason: SkipReason, detail: str
    ) -> None:
        """Record a provider that was NOT called.

        A ``provider_calls`` row and nothing else: no spend (no call was made,
        so there is nothing to charge) and no breaker transition (there is no
        outcome to judge the provider on). This is what makes "an eligibility
        refusal consumes no budget and does not affect any breaker" true by
        construction for every skip, not just that one.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                _INSERT_CALL_SQL,
                {
                    "run_id": run_id,
                    "provider_id": provider_id,
                    "status": reason,
                    "http_status": None,
                    "latency_ms": None,
                    "cost_usd": None,
                    "attempt": 0,  # zero attempts: nothing left this process
                    "error_detail": detail,
                    "raw_response": Jsonb({"skipped": reason, "detail": detail}),
                },
            )
        log.warning(
            "provider.skipped", run_id=str(run_id), provider_id=provider_id, reason=reason
        )

    async def set_enabled(
        self, provider_id: ProviderId, enabled: bool, *, actor: str, reason: str
    ) -> bool:
        """The kill switch. Provider row + audit row, one transaction.

        Returns False for an unknown provider so the route can 404 rather than
        report a successful disable of something that does not exist.
        """
        action = PROVIDER_ENABLED_ACTION if enabled else PROVIDER_DISABLED_ACTION
        changed = await self._write_with_audit(
            _SET_ENABLED_SQL,
            {"provider_id": provider_id, "enabled": enabled},
            action=action,
            metadata={"provider_id": provider_id, "actor": actor, "reason": reason},
        )
        if changed:
            log.warning(
                "provider.kill_switch", provider_id=provider_id, enabled=enabled,
                actor=actor, reason=reason,
            )
        return changed

    async def reset_breaker(
        self, provider_id: ProviderId, *, actor: str, reason: str
    ) -> bool:
        changed = await self._write_with_audit(
            _RESET_BREAKER_SQL,
            {"provider_id": provider_id},
            action=BREAKER_RESET_ACTION,
            metadata={"provider_id": provider_id, "actor": actor, "reason": reason},
        )
        if changed:
            log.warning(
                "provider.breaker_reset", provider_id=provider_id, actor=actor,
                reason=reason,
            )
        return changed

    # ── internals ────────────────────────────────────────────────────────

    async def _write_with_audit(
        self,
        sql: str,
        params: Mapping[str, Any],
        *,
        action: str,
        metadata: Mapping[str, Any],
    ) -> bool:
        """One transaction for the state change and its audit row.

        Inlined into one method rather than composed from two calls for the same
        reason the calibration store does it (``calibration/store.py``): a kill
        switch flipped with no audit row, or an audit row for a change that did
        not happen, is exactly the half-applied state an incident timeline
        cannot be reconstructed from.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(sql, dict(params))
            row = await cur.fetchone()
            if row is None:
                return False
            await conn.execute(
                _AUDIT_SQL, {"action": action, "metadata": Jsonb(dict(metadata))}
            )
        self.invalidate()
        return True

    @staticmethod
    async def _apply_breaker(
        conn: AsyncConnection[Any], provider_id: ProviderId, change: BreakerTransition
    ) -> None:
        await conn.execute(
            _APPLY_BREAKER_SQL,
            {
                "provider_id": provider_id,
                "state": change.state,
                "consecutive_failures": change.consecutive_failures,
                "cooldown_seconds": change.cooldown_seconds,
                "reason": change.reason,
                "opened_at": change.opened_at,
            },
        )


def _breaker_reason(result: ProviderResult) -> str | None:
    """A short, stable reason string for ``providers.breaker_reason``.

    Never the raw provider body: that lands in ``provider_calls.raw_response``
    where it belongs and is subject to the retention job. This column is read
    by an operator on an alarm and by nobody else.
    """
    if classify(result.status) != "failure":
        return None
    if result.http_status is not None:
        return f"{result.status} (http {result.http_status})"
    return result.status


def spend_or_zero(spend: DailySpend | None) -> Decimal:
    """Today's spend, treating "no row yet" as zero rather than as unknown.

    A missing ``provider_spend`` row means no calls today, which is genuinely
    zero — the row is created by the first call.
    """
    return spend.cost_usd if spend is not None else Decimal("0")


def utc_spend_date(now: datetime) -> date:
    """The ``spend_date`` a call made at ``now`` is charged to.

    UTC, deliberately and not a local timezone: ``provider_spend`` is compared
    against a daily budget, and a budget whose day boundary moves with daylight
    saving has two 23-hour days and two 25-hour days a year. A naive datetime is
    taken as already-UTC rather than rejected — every caller in this repo passes
    ``datetime.now(UTC)``, and refusing here would turn a clock detail into a
    failed run.
    """
    return now.astimezone(UTC).date() if now.tzinfo is not None else now.date()
