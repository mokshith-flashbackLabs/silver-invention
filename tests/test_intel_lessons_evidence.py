"""Published evidence after approval (spec 2026-10-15-intel-lessons-and-evidence §6)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.backfill_evidence import backfill
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.evidence_store import PostgresEvidenceStore
from tests.intel_fakes import (
    NOW,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
)


async def _scalar(pool: AsyncConnectionPool, sql: str, *args: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        row = await cur.fetchone()
        return None if row is None else row[0]


async def _signals(pool: AsyncConnectionPool) -> list[UUID]:
    return [
        await seed_signal(pool, tags=("instagram",), publisher="a.example", category="incident"),
        await seed_signal(pool, tags=("instagram",), publisher="b.example", category="incident"),
    ]


async def _approved(pool: AsyncConnectionPool) -> tuple[UUID, list[UUID]]:
    await seed_quiz_vocabulary(pool)
    signals = await _signals(pool)
    proposal = await seed_threat_proposal(pool, signal_ids=signals)
    decided = await PostgresDecisionStore(pool, threat_recency_days=90, clock=lambda: NOW).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op"
    )
    return UUID(str(decided.applied_ref)), signals


async def test_a_retraction_removes_its_rows_and_lowers_the_count(
    intel_pool: AsyncConnectionPool,
) -> None:
    event, signals = await _approved(intel_pool)
    await PostgresEvidenceStore(intel_pool).retract_signal(
        signals[0], operator="Op", reason="wrong page"
    )
    assert (
        await _scalar(
            intel_pool,
            "SELECT independent_sources FROM intel_published_owners WHERE owner_id = %s",
            event,
        )
        == 1
    )
    assert (
        await _scalar(
            intel_pool,
            "SELECT count(DISTINCT publisher_domain) FROM svc.v_evidence WHERE owner_id = %s",
            event,
        )
        == 1
    )


async def test_retracting_the_only_evidence_leaves_the_event_live(
    intel_pool: AsyncConnectionPool,
) -> None:
    event, signals = await _approved(intel_pool)
    for signal in signals:
        await PostgresEvidenceStore(intel_pool).retract_signal(
            signal, operator="Op", reason="wrong"
        )
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM svc.v_evidence WHERE owner_id = %s", event)
        == 0
    )
    assert await _scalar(intel_pool, "SELECT independent_sources FROM intel_published_owners") == 0
    status = await _scalar(
        intel_pool, "SELECT status FROM threat_events WHERE event_id = %s", event
    )
    assert status == "active"


async def test_the_backfill_snapshots_old_events_once(intel_pool: AsyncConnectionPool) -> None:
    signals = await _signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, status="applied", decided={"seeded": True}
    )
    await seed_threat_event(intel_pool, proposal_id=proposal)
    assert await backfill(intel_pool) == 1
    assert await backfill(intel_pool) == 0
    assert await _scalar(intel_pool, "SELECT independent_sources FROM intel_published_owners") == 2
