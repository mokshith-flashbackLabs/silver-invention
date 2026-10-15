"""Lesson and published-evidence SQL (spec 2026-10-15-intel-lessons-and-evidence §5, §6). Every
function that writes takes the caller's connection, so a decision writes its event, its lesson and
its evidence snapshot in ONE transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from imageshield.intel.corroboration import independent_sources
from imageshield.intel.proposal_models import ContextSignal

LessonKind = Literal["threat", "protection", "general"]


async def event_kind(
    conn: AsyncConnection[Any], event_id: UUID
) -> Literal["threat", "protection"] | None:
    """Which table holds this event id, or None when neither does."""
    cur = await conn.execute(
        "SELECT 'threat' FROM threat_events WHERE event_id = %s"
        " UNION ALL SELECT 'protection' FROM protection_events WHERE event_id = %s",
        (event_id, event_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    kind: Literal["threat", "protection"] = row[0]
    return kind


async def insert_lesson(
    conn: AsyncConnection[Any],
    *,
    proposal_id: UUID,
    event_id: UUID | None,
    kind: LessonKind,
    topic: str | None,
    decided: dict[str, Any],
    operator: str,
) -> UUID:
    """An approved lesson. An event's earlier live lesson is retired first ('replaced'), so the
    one-live-per-event index holds and the newest approval is what people read."""
    if event_id is not None:
        await conn.execute(
            "UPDATE intel_lessons SET retired_by = %s, retired_at = now(),"
            " retire_reason = 'replaced' WHERE event_id = %s AND retired_at IS NULL",
            (operator, event_id),
        )
    lesson = decided["lesson"]
    link = lesson.get("link")
    cur = await conn.execute(
        "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro, sections,"
        " link_url, link_label, tags, is_global, approved_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s) RETURNING lesson_id",
        (
            proposal_id,
            event_id,
            kind,
            topic,
            lesson["title"],
            lesson["intro"],
            Jsonb(lesson["sections"]),
            link["url"] if link else None,
            link["label"] if link else None,
            list(decided.get("tags", [])),
            bool(decided.get("is_global", False)),
            operator,
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    lesson_id: UUID = row[0]
    return lesson_id


async def carry_lesson(
    conn: AsyncConnection[Any],
    *,
    from_event_id: UUID,
    to_event_id: UUID,
    proposal_id: UUID,
    operator: str,
) -> UUID | None:
    """A renewal keeps the renewed credit's live lesson (spec §4), on the new event."""
    cur = await conn.execute(
        "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro, sections,"
        " link_url, link_label, tags, is_global, approved_by)"
        " SELECT %s, %s, kind, topic, title, intro, sections, link_url, link_label, tags,"
        " is_global, %s FROM intel_lessons WHERE event_id = %s AND retired_at IS NULL"
        " RETURNING lesson_id",
        (proposal_id, to_event_id, operator, from_event_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    lesson_id: UUID = row[0]
    return lesson_id


async def snapshot_evidence(
    conn: AsyncConnection[Any],
    *,
    owner_kind: Literal["event", "lesson"],
    owner_id: UUID,
    proposal_id: UUID,
    active: Sequence[ContextSignal],
) -> None:
    """What the operator approved on, frozen with the approval (spec §6): every quote of the
    proposal's active signals, and how many independent sources they come from."""
    await conn.execute(
        "INSERT INTO intel_published_owners (owner_kind, owner_id, proposal_id,"
        " independent_sources, approved_at) VALUES (%s, %s, %s, %s, now())"
        " ON CONFLICT (owner_kind, owner_id) DO NOTHING",
        (owner_kind, owner_id, proposal_id, independent_sources(active)),
    )
    await conn.execute(
        "INSERT INTO intel_published_evidence (owner_kind, owner_id, signal_id, excerpt_id)"
        " SELECT %s, %s, e.signal_id, e.excerpt_id FROM intel_proposal_signals ps"
        " JOIN intel_signals s ON s.signal_id = ps.signal_id AND s.status = 'active'"
        " JOIN intel_excerpts e ON e.signal_id = s.signal_id"
        " WHERE ps.proposal_id = %s ON CONFLICT DO NOTHING",
        (owner_kind, owner_id, proposal_id),
    )


async def recompute_after_retraction(conn: AsyncConnection[Any], signal_id: UUID) -> int:
    """Every owner citing a just-retracted signal gets its independence count recomputed from its
    proposal's still-active evidence, in the retraction's transaction (spec §6). The rows stay;
    svc.v_evidence drops them because it joins active signals only. Returns the owners updated."""
    # Imported here: proposal_store is the heavier module and nothing it imports needs this one.
    from imageshield.intel.proposal_store import fetch_linked_signals

    cur = await conn.execute(
        "SELECT DISTINCT o.owner_kind, o.owner_id, o.proposal_id FROM intel_published_evidence pe"
        " JOIN intel_published_owners o"
        "   ON o.owner_kind = pe.owner_kind AND o.owner_id = pe.owner_id"
        " WHERE pe.signal_id = %s",
        (signal_id,),
    )
    owners = await cur.fetchall()
    if not owners:
        return 0
    linked = await fetch_linked_signals(conn, list({row[2] for row in owners}))
    for kind, owner, proposal in owners:
        active = [s for s in linked[proposal] if s.status == "active"]
        await conn.execute(
            "UPDATE intel_published_owners SET independent_sources = %s"
            " WHERE owner_kind = %s AND owner_id = %s",
            (independent_sources(active), kind, owner),
        )
    return len(owners)
