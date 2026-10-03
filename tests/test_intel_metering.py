"""Metering model calls through the existing provider gate (task 9).

Two things this file has to prove that the search-side provider tests
already cover for image_search providers: the inverted budget rule for kind
llm (a NULL budget REFUSES here, where it would mean "uncapped" for a search
provider), and that a model's own outcome vocabulary (refusal/max_tokens/
unparseable) never touches the breaker -- only a genuine transport failure
does.

And, since 2026-10-03, that each way a call ends is one row of the run log when the
worker has bound one (spec 2026-10-03 §3.5), and nothing at all when it has not.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import anthropic
import httpx2
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.metering import metered
from imageshield.intel.model import ModelCall, ModelUnavailable
from imageshield.intel.pricing import Usage
from imageshield.intel.run_log import RunLog, current_run_log
from imageshield.intel.schemas import ExtractionOutput
from imageshield.intel.store import PostgresIntelStore
from imageshield.providers.models import ProviderDailyStats
from imageshield.providers.observability import alarms
from imageshield.providers.store import PostgresProviderControlStore
from tests.db import run_migrate
from tests.intel_fakes import MemoryRunEvents
from tests.test_intel_model import _ALL_ENV_NAMES
from tests.test_intel_model import _model as _intel_model

NOW = datetime.now(UTC)


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE providers SET enabled = true, daily_budget_usd = 5,"
            " cost_per_call_usd = 0.5 WHERE provider_id = 'claude_intel'"
        )
    return throwaway_db


@pytest.fixture
async def pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    p = make_async_pool(migrated_db, min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """test_intel_model's isolation from a developer .env.local, for the one test here that
    builds the real model seam."""
    monkeypatch.chdir(tmp_path)
    for key in _ALL_ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _control(pool: AsyncConnectionPool) -> PostgresProviderControlStore:
    return PostgresProviderControlStore(
        pool,
        cache_seconds=0.0,
        failure_threshold=5,
        default_cooldown_seconds=300,
        max_cooldown_seconds=3600,
    )


def _call(outcome: str = "ok", cost: str = "0.02") -> ModelCall[ExtractionOutput]:
    return ModelCall(
        ExtractionOutput(signals=[]) if outcome == "ok" else None,  # type: ignore[arg-type]
        outcome,  # type: ignore[arg-type]
        "claude-sonnet-5",
        "end_turn",
        Usage(10, 10, 0, 0, 0),
        Decimal(cost),
        5,
    )


async def test_a_call_records_actual_cost_against_the_intel_run(
    pool: AsyncConnectionPool,
) -> None:
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")

    async def call() -> ModelCall[ExtractionOutput]:
        return _call(cost="0.02")

    outcome, result, reason = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert outcome == "called" and result is not None and reason is None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT intel_run_id, run_id, status, cost_usd, raw_response FROM provider_calls"
        )
        (intel_run_id, search_run, status, cost, raw) = await cur.fetchone()  # type: ignore[misc]
    assert intel_run_id == run_id and search_run is None and status == "ok"
    assert cost == Decimal("0.0200")
    assert set(raw) <= {
        "model",
        "stop_reason",
        "usage",
        "web_search_requests",
        "pause_turns",
        "outcome",
    }


async def test_refusal_is_ok_and_never_opens_the_breaker(pool: AsyncConnectionPool) -> None:
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")

    async def call() -> ModelCall[ExtractionOutput]:
        return _call(outcome="refusal")

    for _ in range(6):
        await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT breaker_state FROM providers WHERE provider_id = 'claude_intel'"
        )
        assert await cur.fetchone() == ("closed",)


async def test_timeouts_are_recorded_and_can_open_the_breaker(pool: AsyncConnectionPool) -> None:
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")

    async def call() -> ModelCall[ExtractionOutput]:
        raise ModelUnavailable("timeout", "APITimeoutError")

    for _ in range(5):
        outcome, result, reason = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
        assert outcome == "called" and result is None and reason == "timeout"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT breaker_state FROM providers WHERE provider_id = 'claude_intel'"
        )
        assert await cur.fetchone() == ("open",)


async def test_no_budget_refuses_before_any_call(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET daily_budget_usd = NULL WHERE provider_id = 'claude_intel'"
        )
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")
    called = False

    async def call() -> ModelCall[ExtractionOutput]:
        nonlocal called
        called = True
        return _call()

    outcome, _, _ = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert outcome == "budget_unset" and not called


async def test_an_ordinary_budget_exceeded_provider_is_skipped_not_refused(
    pool: AsyncConnectionPool,
) -> None:
    """Distinct from budget_unset: a REAL budget that has simply been spent
    goes through the ordinary gate.decide path (status budget_exceeded), not
    the special-cased "no budget configured at all" refusal."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET daily_budget_usd = 0.01 WHERE provider_id = 'claude_intel'"
        )
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")
    called = False

    async def call() -> ModelCall[ExtractionOutput]:
        nonlocal called
        called = True
        return _call()

    outcome, result, reason = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert outcome == "skipped" and result is None and reason == "budget_exceeded"
    assert not called
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT intel_run_id, status FROM provider_calls WHERE status = 'budget_exceeded'"
        )
        (intel_run_id, status) = await cur.fetchone()  # type: ignore[misc]
    assert intel_run_id == run_id and status == "budget_exceeded"


# ── alarms (pure) ──────────────────────────────────────────────────────────


def _stats(**kw: object) -> ProviderDailyStats:
    base: dict[str, Any] = dict(
        provider_id="claude_intel",
        enabled=True,
        breaker_state="closed",
        breaker_reason=None,
        call_count=0,
        cost_usd=Decimal("0"),
        daily_budget_usd=Decimal("5"),
        monthly_budget_usd=None,
        month_to_date_cost_usd=Decimal("0"),
        budget_headroom_usd=Decimal("5"),
        success_rate=None,
        window_call_count=0,
        successful_calls_24h=0,
        latency_p50_ms=None,
        latency_p99_ms=None,
        kind="llm",
        intel_overdue=False,
    )
    base.update(kw)
    return ProviderDailyStats.model_validate(base)


def test_llm_never_raises_no_successful_calls_but_raises_intel_stale_when_overdue() -> None:
    kinds = {a.kind for a in alarms(_stats(), spend_alarm_fraction=0.8, success_rate_alarm=0.9)}
    assert "no_successful_calls_24h" not in kinds
    assert "intel_stale" not in kinds

    kinds = {
        a.kind
        for a in alarms(
            _stats(intel_overdue=True), spend_alarm_fraction=0.8, success_rate_alarm=0.9
        )
    }
    assert "intel_stale" in kinds
    assert "no_successful_calls_24h" not in kinds


# ── the run log (spec 2026-10-03 §3.5) ─────────────────────────────────────────────────────


@pytest.fixture
async def logged(pool: AsyncConnectionPool) -> AsyncIterator[tuple[UUID, PostgresIntelStore]]:
    """A run with a run log bound, as the worker binds one around `run(claimed, deps)`."""
    store = PostgresIntelStore(pool)
    run_id = await store.queue_adhoc("https://n.example/a", operator="a")
    token = current_run_log.set(RunLog(run_id, store))
    try:
        yield run_id, store
    finally:
        current_run_log.reset(token)


async def _events(store: PostgresIntelStore, run_id: UUID) -> list[tuple[str, str, dict[str, Any]]]:
    read = await store.run_events(run_id)
    assert read is not None
    return [(e.kind, e.text, e.detail) for e in read.events]


async def test_an_answer_is_model_call_finished(
    pool: AsyncConnectionPool, logged: tuple[UUID, PostgresIntelStore]
) -> None:
    run_id, store = logged

    async def call() -> ModelCall[ExtractionOutput]:
        return ModelCall(
            ExtractionOutput(signals=[]),
            "ok",
            "claude-sonnet-5",
            "end_turn",
            Usage(1200, 300, 0, 0, 2),
            Decimal("0.2345"),
            5,
        )

    outcome, result, _ = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert outcome == "called" and result is not None
    ((kind, text, detail),) = await _events(store, run_id)
    assert kind == "model_call_finished"
    assert re.fullmatch(r"Answered in \d+\.\ds · \$0\.23 · 2 searches", text), text
    assert detail == {
        "model": "claude-sonnet-5",
        "stop_reason": "end_turn",
        "input_tokens": 1200,
        "output_tokens": 300,
        "web_search_requests": 2,
        "cost_usd": "0.2345",
        "latency_ms": detail["latency_ms"],
    }
    assert isinstance(detail["latency_ms"], int)


async def test_an_unavailable_model_is_model_call_failed_and_error_detail_is_unchanged(
    pool: AsyncConnectionPool, logged: tuple[UUID, PostgresIntelStore], clean_env: Any
) -> None:
    """The 2026-10-03 failure, end to end: a 403 refused by the stream, through the real model
    seam and the gate. provider_calls.error_detail reads exactly as it did; the run log names
    the API's reason, which nothing recorded before."""
    run_id, store = logged
    reason = "User is not authorized to perform: sts:GetWebIdentityToken"
    request = httpx2.Request("POST", "https://anthropic.aws.example/v1/messages")
    refused = anthropic.PermissionDeniedError(
        "Error code: 403",
        response=httpx2.Response(403, request=request),
        body={"type": "error", "error": {"type": "permission_error", "message": reason}},
    )
    model, _ = _intel_model([refused], clean_env)
    outcome, result, reason_code = await metered(
        _control(pool), run_id=run_id, now=NOW, call=lambda: model.propose("s", "u")
    )
    assert (outcome, result, reason_code) == ("called", None, "error")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, error_detail FROM provider_calls WHERE intel_run_id = %s", (run_id,)
        )
        assert await cur.fetchone() == ("error", "PermissionDeniedError:403")
    (started, failed) = await _events(store, run_id)
    assert started[0] == "model_call_started" and started[2]["model"] == "claude-opus-5-5"
    assert failed == (
        "model_call_failed",
        f"Claude call failed: permission denied (403) — {reason}",
        {
            "status": "error",
            "error_type": "PermissionDeniedError",
            "http_status": 403,
            "message": reason,
        },
    )


@pytest.mark.parametrize(
    ("budget", "enabled", "reason", "text"),
    [
        ("NULL", True, "budget_unset", "Not sent: no daily budget is set"),
        ("0.01", True, "budget_exceeded", "Not sent: today's budget is spent"),
        ("5", False, "provider_disabled", "Not sent: the provider is switched off"),
    ],
)
async def test_a_refusal_before_the_call_is_model_call_skipped(
    pool: AsyncConnectionPool,
    logged: tuple[UUID, PostgresIntelStore],
    budget: str,
    enabled: bool,
    reason: str,
    text: str,
) -> None:
    run_id, store = logged
    async with pool.connection() as conn:
        await conn.execute(
            f"UPDATE providers SET daily_budget_usd = {budget}, enabled = %s"
            " WHERE provider_id = 'claude_intel'",
            (enabled,),
        )

    async def call() -> ModelCall[ExtractionOutput]:
        raise AssertionError("never called")

    await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert await _events(store, run_id) == [("model_call_skipped", text, {"reason": reason})]


async def test_with_no_log_bound_metering_writes_no_row_and_still_works(
    pool: AsyncConnectionPool,
) -> None:
    assert current_run_log.get() is None
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")

    async def call() -> ModelCall[ExtractionOutput]:
        raise ModelUnavailable("timeout", "APITimeoutError")

    outcome, _, reason = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    assert (outcome, reason) == ("called", "timeout")
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM intel_run_events")
        assert await cur.fetchone() == (0,)


async def test_a_log_that_cannot_be_written_leaves_metering_unchanged(
    pool: AsyncConnectionPool,
) -> None:
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://n.example/a", operator="a")
    token = current_run_log.set(RunLog(run_id, MemoryRunEvents(fail=True)))
    try:

        async def call() -> ModelCall[ExtractionOutput]:
            return _call(cost="0.02")

        outcome, result, reason = await metered(_control(pool), run_id=run_id, now=NOW, call=call)
    finally:
        current_run_log.reset(token)
    assert outcome == "called" and result is not None and reason is None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, cost_usd FROM provider_calls WHERE intel_run_id = %s", (run_id,)
        )
        assert await cur.fetchone() == ("ok", Decimal("0.0200"))
