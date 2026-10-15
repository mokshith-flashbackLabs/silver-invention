"""Published evidence after approval (spec 2026-10-15-intel-lessons-and-evidence §6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.backfill_evidence import backfill
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.evidence_store import PostgresEvidenceStore
from tests.intel_fakes import (
    NOW,
    THREAT_SUGGESTED,
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


async def _approved_with_lesson(
    pool: AsyncConnectionPool, lesson: dict[str, Any]
) -> tuple[UUID, list[UUID]]:
    """A threat approved with ``lesson``, on two signals: A quotes "48 hours" from a vetted page,
    B carries no figure."""
    await seed_quiz_vocabulary(pool)
    signals = [
        await seed_signal(
            pool,
            tags=("instagram",),
            publisher="a.example",
            category="incident",
            url="https://a.example/guide",
            quote="Requests to remove an image are reviewed within 48 hours by the platform.",
        ),
        await seed_signal(
            pool,
            tags=("instagram",),
            publisher="b.example",
            category="incident",
            quote="A fake sign-in page then steals the account of the channel owner.",
        ),
    ]
    proposal = await seed_threat_proposal(
        pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": lesson}
    )
    decided = await PostgresDecisionStore(pool, threat_recency_days=90, clock=lambda: NOW).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op"
    )
    return UUID(str(decided.applied_ref)), signals


async def test_a_retraction_retires_a_lesson_that_rested_on_the_retracted_article(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Spec §6: a retracted article never reaches a report, through a lesson's link or a figure
    only it quoted. The lesson is retired in the retraction's transaction."""
    event, signals = await _approved_with_lesson(
        intel_pool,
        {
            "title": "Ask the platform to remove it",
            "intro": "A request is reviewed within 48 hours.",
            "sections": [{"kind": "how_to_use", "items": ["Use the platform's own form."]}],
            "link": {"url": "https://a.example/guide", "label": "The platform's guide"},
        },
    )
    await PostgresEvidenceStore(intel_pool).retract_signal(
        signals[0], operator="Op", reason="the page was compromised"
    )
    retired = await _scalar(
        intel_pool, "SELECT retire_reason FROM intel_lessons WHERE event_id = %s", event
    )
    assert retired == "evidence_retracted"
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM svc.v_lessons WHERE event_id = %s", event)
        == 0
    )


async def test_a_lesson_stays_while_its_remaining_evidence_carries_it_and_goes_with_the_last(
    intel_pool: AsyncConnectionPool,
) -> None:
    event, signals = await _approved_with_lesson(
        intel_pool,
        {
            "title": "Spot a fake sign-in page",
            "intro": "A fake page asks you to sign in again.",
            "sections": [{"kind": "what_to_do", "items": ["Sign in only from the app itself."]}],
            "link": None,
        },
    )
    store = PostgresEvidenceStore(intel_pool)
    await store.retract_signal(signals[0], operator="Op", reason="wrong page")
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM svc.v_lessons WHERE event_id = %s", event)
        == 1
    )
    await store.retract_signal(signals[1], operator="Op", reason="wrong page")
    assert (
        await _scalar(
            intel_pool, "SELECT retire_reason FROM intel_lessons WHERE event_id = %s", event
        )
        == "evidence_retracted"
    )


async def test_the_backfill_dates_a_snapshot_by_its_approval_not_by_the_backfill(
    intel_pool: AsyncConnectionPool,
) -> None:
    """"Approved by our team on <date>" is shown to people: an event approved before 0055 keeps
    the date it was approved, never the day the backfill ran."""
    approved = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    signals = await _signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, status="applied", decided={"seeded": True}
    )
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_proposals SET decided_at = %s WHERE proposal_id = %s",
            (approved, proposal),
        )
    await seed_threat_event(intel_pool, proposal_id=proposal)
    assert await backfill(intel_pool) == 1
    assert await _scalar(intel_pool, "SELECT approved_at FROM intel_published_owners") == approved
