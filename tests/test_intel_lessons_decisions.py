"""Deciding lessons (spec 2026-10-15-intel-lessons-and-evidence §4-§6), against real Postgres."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.proposal_models import DecisionRefused
from tests.intel_fakes import (
    NOW,
    THREAT_SUGGESTED,
    seed_proposal,
    seed_protection_event,
    seed_protection_proposal,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_proposal,
)

LESSON: dict[str, Any] = {
    "title": "Spot a fake sponsorship offer",
    "intro": "A fake brand deal leads to a sign-in page that steals the account.",
    "sections": [{"kind": "what_to_do", "items": ["Sign in only from the app itself."]}],
    "link": None,
}


def _store(pool: AsyncConnectionPool) -> PostgresDecisionStore:
    return PostgresDecisionStore(pool, threat_recency_days=90, clock=lambda: NOW)


async def _two_signals(pool: AsyncConnectionPool) -> list[UUID]:
    """The live quiz (Instagram mapped to instagram, so an event on it reaches somebody) and two
    corroborating signals on it."""
    await seed_quiz_vocabulary(pool)
    # Fixed quotes with no digit: unique_quote's hex words can carry one, which would make a
    # figure in a test lesson read as quoted by chance.
    return [
        await seed_signal(
            pool,
            tags=("instagram",),
            publisher="a.example",
            category="incident",
            quote="Scammers pose as known brands to reach creators with offers.",
        ),
        await seed_signal(
            pool,
            tags=("instagram",),
            publisher="b.example",
            category="incident",
            quote="A fake sign-in page then steals the account of the channel owner.",
        ),
    ]


async def _rows(pool: AsyncConnectionPool, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def _lesson_on(pool: AsyncConnectionPool, event_id: UUID, title: str) -> None:
    """A live lesson on an existing event, written directly (the event's own proposal)."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections,"
            " approved_by) SELECT proposal_id, event_id, 'protection', %s, 'i', %s, 'op'"
            " FROM protection_events WHERE event_id = %s",
            (title, Jsonb(LESSON["sections"]), event_id),
        )


async def test_a_threat_approval_writes_its_lesson_and_snapshots_both(
    intel_pool: AsyncConnectionPool,
) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    decided = await _store(intel_pool).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op",
        applies_regardless_of_location=True,
    )
    event_id = UUID(str(decided.applied_ref))
    lessons = await _rows(
        intel_pool, "SELECT kind, title FROM intel_lessons WHERE event_id = %s", event_id
    )
    assert lessons == [("threat", "Spot a fake sponsorship offer")]
    owners = await _rows(
        intel_pool,
        "SELECT owner_kind, independent_sources FROM intel_published_owners ORDER BY owner_kind",
    )
    assert owners == [("event", 2), ("lesson", 2)]
    shown = await _rows(
        intel_pool, "SELECT count(*) FROM svc.v_evidence WHERE owner_id = %s", event_id
    )
    assert shown[0][0] >= 2


async def test_values_lesson_null_writes_no_lesson(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    await _store(intel_pool).decide(
        proposal, decision="approved", values={"lesson": None}, reason="ok", operator="Op"
    )
    assert await _rows(intel_pool, "SELECT 1 FROM intel_lessons") == []
    assert len(await _rows(intel_pool, "SELECT 1 FROM intel_published_owners")) == 1


async def test_an_edited_lesson_that_breaks_a_rule_is_refused(
    intel_pool: AsyncConnectionPool,
) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(intel_pool, signal_ids=signals)
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            proposal,
            decision="approved",
            reason="ok",
            operator="Op",
            values={"lesson": {**LESSON, "intro": "You are safe after 9 days."}},
            applies_regardless_of_location=True,
        )
    assert refused.value.code == "lesson_invalid"
    assert {"field": "intro", "rule": "reassurance"} in refused.value.problems
    assert {"field": "intro", "rule": "figure_not_quoted"} in refused.value.problems
    assert await _rows(intel_pool, "SELECT 1 FROM threat_events") == []


async def test_a_general_lesson_approval_is_applied_with_its_lesson_id(
    intel_pool: AsyncConnectionPool,
) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"topic": "fake_sponsorships"},
        suggested={"lesson": LESSON, "tags": ["instagram"], "is_global": False},
    )
    decided = await _store(intel_pool).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op",
        applies_regardless_of_location=True,
    )
    assert decided.status == "applied"
    rows = await _rows(intel_pool, "SELECT lesson_id, kind, topic FROM intel_lessons")
    assert rows == [(UUID(str(decided.applied_ref)), "general", "fake_sponsorships")]
    assert len(await _rows(intel_pool, "SELECT 1 FROM svc.v_lessons")) == 1


async def test_an_unscoped_general_lesson_is_refused(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"topic": "fake_sponsorships"},
        suggested={"lesson": LESSON, "tags": [], "is_global": False},
    )
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            proposal, decision="approved", values=None, reason="ok", operator="Op",
        applies_regardless_of_location=True,
        )
    assert refused.value.code == "values_out_of_bounds"


async def test_a_lesson_for_an_event_replaces_its_live_lesson(
    intel_pool: AsyncConnectionPool,
) -> None:
    signals = await _two_signals(intel_pool)
    event_proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    event = (
        await _store(intel_pool).decide(
            event_proposal, decision="approved", values=None, reason="ok", operator="Op",
        applies_regardless_of_location=True,
        )
    ).applied_ref
    redraft = await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"event_id": event},
        suggested={
            "lesson": {**LESSON, "title": "A better lesson"},
            "tags": [],
            "is_global": False,
        },
    )
    await _store(intel_pool).decide(
        redraft, decision="approved", values=None, reason="ok", operator="Op",
        applies_regardless_of_location=True,
    )
    live = await _rows(
        intel_pool, "SELECT title FROM svc.v_lessons WHERE event_id = %s", UUID(str(event))
    )
    assert live == [("A better lesson",)]


async def test_a_renewal_carries_the_lesson_and_a_null_drops_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    signals = await _two_signals(intel_pool)
    for values, expected in ((None, [("Use the removal form",)]), ({"lesson": None}, [])):
        credit = await seed_protection_event(intel_pool, ends_in_days=10)
        await _lesson_on(intel_pool, credit, "Use the removal form")
        renewal = await seed_protection_proposal(intel_pool, signal_ids=signals, renews=credit)
        decided = await _store(intel_pool).decide(
            renewal,
            decision="approved",
            values=values,
            reason="ok",
            operator="Op",
            applies_regardless_of_location=True,
        )
        rows = await _rows(
            intel_pool,
            "SELECT title FROM intel_lessons WHERE event_id = %s AND retired_at IS NULL",
            UUID(str(decided.applied_ref)),
        )
        assert rows == expected, values


async def test_a_renewal_does_not_carry_a_lesson_its_evidence_no_longer_supports(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Spec §3: every figure in a lesson appears in a quote the renewal cites. A carried lesson
    whose figure only the old evidence quoted is not carried; the operator never touched it, so
    the approval is not refused either."""
    signals = await _two_signals(intel_pool)
    credit = await seed_protection_event(intel_pool, ends_in_days=10)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections,"
            " approved_by) SELECT proposal_id, event_id, 'protection', 'Use the removal form',"
            " 'A request is reviewed within 48 hours.', %s, 'op'"
            " FROM protection_events WHERE event_id = %s",
            (Jsonb(LESSON["sections"]), credit),
        )
    renewal = await seed_protection_proposal(intel_pool, signal_ids=signals, renews=credit)
    decided = await _store(intel_pool).decide(
        renewal,
        decision="approved",
        values=None,
        reason="ok",
        operator="Op",
        applies_regardless_of_location=True,
    )
    rows = await _rows(
        intel_pool,
        "SELECT title FROM intel_lessons WHERE event_id = %s",
        UUID(str(decided.applied_ref)),
    )
    assert rows == []



# -- 2026-10-22: the location tick, and two sources for a lesson --


async def test_an_approval_publishing_a_lesson_needs_the_location_tick(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Owner, 2026-10-22: the Take It Down Act lesson (a US law) was drafted for everyone. A lesson
    is published only when the operator attests it holds wherever a person lives, as a protection
    is; an approval that publishes none needs no tick."""
    signals = await _two_signals(intel_pool)
    threat = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            threat, decision="approved", values=None, reason="ok", operator="Op"
        )
    assert refused.value.code == "values_out_of_bounds"
    assert "applies_regardless_of_location" in refused.value.message
    assert await _rows(intel_pool, "SELECT 1 FROM threat_events") == []

    await _store(intel_pool).decide(
        threat, decision="approved", values={"lesson": None}, reason="ok", operator="Op"
    )
    assert await _rows(intel_pool, "SELECT 1 FROM intel_lessons") == []

    general = await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"topic": "fake_sponsorships"},
        suggested={"lesson": LESSON, "tags": ["instagram"], "is_global": False},
    )
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            general,
            decision="approved",
            values=None,
            reason="ok",
            operator="Op",
            applies_regardless_of_location=False,
        )
    assert refused.value.code == "values_out_of_bounds"
    assert await _rows(intel_pool, "SELECT 1 FROM intel_lessons") == []


async def _one_registered_source(pool: AsyncConnectionPool) -> list[UUID]:
    await seed_quiz_vocabulary(pool)
    return [
        await seed_signal(
            pool,
            tags=("instagram",),
            publisher="a.example",
            trust="listed",
            category="incident",
            quote="Scammers pose as known brands to reach creators with offers.",
        )
    ]


async def test_a_lesson_on_one_registered_source_is_never_approved(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Owner, 2026-10-22: one registered source carries a threat, never its lesson."""
    signals = await _one_registered_source(intel_pool)
    general = await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"topic": "fake_sponsorships"},
        suggested={"lesson": LESSON, "tags": ["instagram"], "is_global": False},
    )
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            general,
            decision="approved",
            values=None,
            reason="ok",
            operator="Op",
            applies_regardless_of_location=True,
        )
    assert refused.value.code == "proposal_uncorroborated"

    threat = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            threat,
            decision="approved",
            values=None,
            reason="ok",
            operator="Op",
            applies_regardless_of_location=True,
        )
    assert refused.value.code == "lesson_invalid"
    assert {"field": "lesson", "rule": "uncorroborated"} in refused.value.problems
    decided = await _store(intel_pool).decide(
        threat, decision="approved", values={"lesson": None}, reason="ok", operator="Op"
    )
    assert decided.status == "applied"
    assert await _rows(intel_pool, "SELECT 1 FROM intel_lessons") == []


async def test_existing_lessons_lists_live_and_pending_lessons_of_every_origin(
    intel_pool: AsyncConnectionPool,
) -> None:
    """What the model is shown and code checks a new lesson against (2026-10-22): live lessons,
    pending or approved lesson proposals, and the lessons riding on pending event proposals."""
    from imageshield.intel.proposal_models import ExistingLesson
    from imageshield.intel.proposal_store import PostgresProposalStore

    signals = await _two_signals(intel_pool)
    await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    await seed_proposal(
        intel_pool,
        signal_ids=signals,
        kind="lesson",
        target={"topic": "fake_sponsorships"},
        suggested={"lesson": {**LESSON, "title": "Library lesson"}, "tags": [], "is_global": True},
    )
    store = PostgresProposalStore(intel_pool, threat_recency_days=90)
    existing = await store.existing_lessons(limit=50)
    assert set(existing) == {
        ExistingLesson(topic=None, title="Spot a fake sponsorship offer"),
        ExistingLesson(topic="fake_sponsorships", title="Library lesson"),
    }
