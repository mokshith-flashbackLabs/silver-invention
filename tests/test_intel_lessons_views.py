"""The three 0055 views and the topic lock, against real Postgres (spec
2026-10-15-intel-lessons-and-evidence §10: the tests that must exist)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.proposal_models import NewProposal
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import (
    NOW,
    THREAT_SUGGESTED,
    seed_proposal,
    seed_protection_event,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
)

SECTIONS = [{"kind": "what_to_do", "items": ["Sign in only from the app itself."]}]
LESSON = {
    "title": "Spot a fake page",
    "intro": "A fake page asks you to sign in.",
    "sections": SECTIONS,
}


async def _rows(pool: AsyncConnectionPool, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def test_weekly_totals_count_a_fixed_fixture_exactly_week_by_week(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Documents and publishers by fetch week, active risk_up signals by creation week, event and
    lesson proposals by creation week, decisions by decision week."""
    week1 = datetime(2026, 6, 1, 9, tzinfo=UTC)  # a Monday
    week2 = datetime(2026, 6, 10, 9, tzinfo=UTC)  # the next week's Wednesday
    a1 = await seed_signal(intel_pool, publisher="a.example", category="incident")
    a2 = await seed_signal(intel_pool, publisher="a.example", category="incident")
    b1 = await seed_signal(intel_pool, publisher="b.example", category="incident")
    gone = await seed_signal(intel_pool, publisher="c.example", category="incident")
    async with intel_pool.connection() as conn:
        for signal, at in ((a1, week1), (a2, week1), (b1, week2), (gone, week2)):
            await conn.execute(
                "UPDATE intel_signals SET created_at = %s WHERE signal_id = %s", (at, signal)
            )
            await conn.execute(
                "UPDATE intel_documents SET fetched_at = %s WHERE document_id ="
                " (SELECT document_id FROM intel_signals WHERE signal_id = %s)",
                (at, signal),
            )
        await conn.execute(
            "UPDATE intel_signals SET status = 'retracted', retracted_by = 'op',"
            " retracted_at = now(), retract_reason = 'x' WHERE signal_id = %s",
            (gone,),
        )
    approved = await seed_proposal(
        intel_pool,
        signal_ids=[a1],
        kind="threat_event",
        status="applied",
        target={"tags": ["instagram"]},
        decided={"seeded": True},
        created_at=week1,
    )
    rejected = await seed_proposal(
        intel_pool,
        signal_ids=[b1],
        kind="lesson",
        status="rejected",
        target={"topic": "spot_fakes"},
        created_at=week2,
    )
    # A weight change is never counted: the view is about events and lessons.
    await seed_proposal(intel_pool, signal_ids=[a2], created_at=week1)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_proposals SET decided_at = %s WHERE proposal_id = %s", (week2, approved)
        )
        await conn.execute(
            "UPDATE intel_proposals SET decided_at = %s WHERE proposal_id = %s", (week2, rejected)
        )
    rows = await _rows(
        intel_pool,
        "SELECT week_start, documents_read, publishers, risks_found, proposed, approved, rejected"
        " FROM svc.v_intel_weekly_totals WHERE week_start IN ('2026-06-01', '2026-06-08')"
        " ORDER BY week_start",
    )
    assert [(str(r[0]), *r[1:]) for r in rows] == [
        ("2026-06-01", 2, 1, 2, 1, 0, 0),
        ("2026-06-08", 2, 2, 1, 1, 1, 1),
    ]


async def test_evidence_links_only_a_vetted_document(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    listed = await seed_signal(
        intel_pool, tags=("instagram",), publisher="a.example", category="incident", trust="listed"
    )
    web = await seed_signal(
        intel_pool, tags=("instagram",), publisher="b.example", category="incident", trust="web"
    )
    proposal = await seed_threat_proposal(intel_pool, signal_ids=[listed, web])
    decided = await PostgresDecisionStore(
        intel_pool, threat_recency_days=90, clock=lambda: NOW
    ).decide(proposal, decision="approved", values=None, reason="ok", operator="Op")
    rows = await _rows(
        intel_pool,
        "SELECT publisher_domain, link_url IS NULL FROM svc.v_evidence WHERE owner_id = %s"
        " ORDER BY publisher_domain",
        UUID(str(decided.applied_ref)),
    )
    assert rows == [("a.example", False), ("b.example", True)]


def test_the_views_are_readable_as_the_proxy_role_and_no_base_table_is(intel_db: str) -> None:
    with psycopg.connect(intel_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_proxy_ro")
        for view in ("v_evidence", "v_lessons", "v_intel_weekly_totals"):
            conn.execute(f"SELECT * FROM svc.{view} LIMIT 1").fetchall()
        for table in ("intel_lessons", "intel_published_owners", "intel_published_evidence"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")


async def test_a_lesson_on_a_topic_already_open_or_live_is_not_written(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Re-checked under the write lock, so a topic another run opened since generation read the
    open topics is never proposed twice."""
    signal = await seed_signal(intel_pool, category="tooling")
    pending = await seed_proposal(
        intel_pool, signal_ids=[signal], kind="lesson", target={"topic": "spot_fakes"}
    )
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, kind, topic, title, intro, sections,"
            " approved_by) VALUES (%s, 'general', 'live_topic', 't', 'i', %s, 'op')",
            (pending, Jsonb(SECTIONS)),
        )
    run = await PostgresIntelStore(intel_pool).queue_adhoc("https://p.example/x", operator="a")
    result = await PostgresProposalStore(intel_pool, threat_recency_days=90).write_generated(
        run,
        [
            NewProposal("lesson", {"topic": topic}, {"lesson": LESSON}, "r", (signal,))
            for topic in ("spot_fakes", "live_topic", "fresh_topic")
        ],
        against_scoring_version="s2",
        against_release_no=2,
        model_id="claude-opus-5-5",
        prompt_version="propose-v14",
    )
    assert result is not None and result.dropped_lessons == 2
    topics = await _rows(
        intel_pool,
        "SELECT target->>'topic', count(*) FROM intel_proposals WHERE kind = 'lesson'"
        " GROUP BY 1 ORDER BY 1",
    )
    assert topics == [("fresh_topic", 1), ("spot_fakes", 1)]


async def test_a_retracted_threat_and_a_protection_past_review_take_their_lessons_away(
    intel_pool: AsyncConnectionPool,
) -> None:
    signal = await seed_signal(intel_pool, category="incident")
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=[signal], status="applied", decided={**THREAT_SUGGESTED}
    )
    threat = await seed_threat_event(intel_pool, proposal_id=proposal)
    protection = await seed_protection_event(intel_pool)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections,"
            " approved_by) VALUES (%s, %s, 'threat', 't', 'i', %s, 'op')",
            (proposal, threat, Jsonb(SECTIONS)),
        )
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections,"
            " approved_by) SELECT proposal_id, event_id, 'protection', 'p', 'i', %s, 'op'"
            " FROM protection_events WHERE event_id = %s",
            (Jsonb(SECTIONS), protection),
        )
    live = "SELECT count(*) FROM svc.v_lessons WHERE event_id = %s"
    assert (await _rows(intel_pool, live, threat))[0][0] == 1
    assert (await _rows(intel_pool, live, protection))[0][0] == 1
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE threat_events SET status = 'retracted' WHERE event_id = %s", (threat,)
        )
        await conn.execute(
            "UPDATE protection_events SET starts_at = now() - interval '20 days',"
            " review_by = now() - interval '1 day' WHERE event_id = %s",
            (protection,),
        )
    assert (await _rows(intel_pool, live, threat))[0][0] == 0
    assert (await _rows(intel_pool, live, protection))[0][0] == 0
