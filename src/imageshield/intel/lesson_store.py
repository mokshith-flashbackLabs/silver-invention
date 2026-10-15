"""Lesson and published-evidence SQL (spec 2026-10-15-intel-lessons-and-evidence §5, §6). Every
function that writes takes the caller's connection, so a decision writes its event, its lesson and
its evidence snapshot in ONE transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from imageshield.intel.corroboration import independent_sources
from imageshield.intel.lessons import lesson_problems
from imageshield.intel.proposal_models import ContextSignal, LessonDraft

log = structlog.get_logger("imageshield.intel")

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


def _still_supported(row: Sequence[Any], active: Sequence[ContextSignal]) -> bool:
    """Whether a stored lesson (title, intro, sections, link_url, link_label) still passes every
    rule against ``active`` (spec §3): the word rules, each figure quoted, the link a cited vetted
    page. A lesson with no active evidence at all rests on nothing, so it is not supported."""
    if not active:
        return False
    title, intro, sections, link_url, link_label = row
    try:
        draft = LessonDraft.model_validate(
            {
                "title": title,
                "intro": intro,
                "sections": sections,
                "link": None if link_url is None else {"url": link_url, "label": link_label},
            }
        )
    except ValidationError:
        return False
    return not lesson_problems(draft, active)


async def carry_lesson(
    conn: AsyncConnection[Any],
    *,
    from_event_id: UUID,
    to_event_id: UUID,
    proposal_id: UUID,
    operator: str,
    active: Sequence[ContextSignal],
) -> UUID | None:
    """A renewal keeps the renewed credit's live lesson (spec §4), on the new event, only when the
    renewal's own active evidence still carries it (§3): a figure or a link only the old evidence
    supported is not carried. The operator never touched the lesson, so a lesson that fails is
    dropped and logged rather than refusing the renewal."""
    cur = await conn.execute(
        "SELECT title, intro, sections, link_url, link_label FROM intel_lessons"
        " WHERE event_id = %s AND retired_at IS NULL",
        (from_event_id,),
    )
    current = await cur.fetchone()
    if current is None:
        return None
    if not _still_supported(current, active):
        log.info("intel.lesson_carry_dropped", from_event_id=str(from_event_id))
        return None
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
    approved_at: datetime | None = None,
) -> None:
    """What the operator approved on, frozen with the approval (spec §6): every quote of the
    proposal's active signals, and how many independent sources they come from. ``approved_at`` is
    shown to people ("Approved by our team on ..."): now() for a decision being made, the recorded
    decision time for the backfill."""
    await conn.execute(
        "INSERT INTO intel_published_owners (owner_kind, owner_id, proposal_id,"
        " independent_sources, approved_at) VALUES (%s, %s, %s, %s, COALESCE(%s, now()))"
        " ON CONFLICT (owner_kind, owner_id) DO NOTHING",
        (owner_kind, owner_id, proposal_id, independent_sources(active), approved_at),
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


async def retire_unsupported_lessons(
    conn: AsyncConnection[Any], signal_id: UUID, *, operator: str
) -> list[UUID]:
    """Every live lesson that cited a just-retracted signal is checked again against its proposal's
    still-active evidence, in the retraction's transaction (spec §6: a retracted article never
    reaches a report). One that no longer passes (a figure or a link only that article carried) or
    one left with no evidence at all is retired as 'evidence_retracted'. Returns their ids."""
    # Imported here: proposal_store is the heavier module and nothing it imports needs this one.
    from imageshield.intel.proposal_store import fetch_linked_signals

    cur = await conn.execute(
        "SELECT DISTINCT l.lesson_id, o.proposal_id, l.title, l.intro, l.sections, l.link_url,"
        " l.link_label FROM intel_published_evidence pe"
        " JOIN intel_published_owners o"
        "   ON o.owner_kind = pe.owner_kind AND o.owner_id = pe.owner_id"
        " JOIN intel_lessons l ON l.lesson_id = o.owner_id"
        " WHERE pe.signal_id = %s AND pe.owner_kind = 'lesson' AND l.retired_at IS NULL",
        (signal_id,),
    )
    lessons = await cur.fetchall()
    if not lessons:
        return []
    linked = await fetch_linked_signals(conn, list({row[1] for row in lessons}))
    retired: list[UUID] = []
    for lesson_id, proposal_id, *stored in lessons:
        active = [s for s in linked[proposal_id] if s.status == "active"]
        if _still_supported(stored, active):
            continue
        await conn.execute(
            "UPDATE intel_lessons SET retired_by = %s, retired_at = now(),"
            " retire_reason = 'evidence_retracted' WHERE lesson_id = %s AND retired_at IS NULL",
            (operator, lesson_id),
        )
        retired.append(lesson_id)
    return retired


RetireOutcome = Literal["retired", "already_retired", "not_found"]

_LESSON_COLUMNS = """lesson_id, proposal_id, event_id, kind, topic, title, intro, sections,
    link_url, link_label, tags, is_global, approved_by, approved_at, retired_by, retired_at,
    retire_reason"""

# Live events with a proposal (so with evidence), no live lesson, and no lesson pending for them.
_DRAFT_EVENTS_SQL = """
    SELECT e.event_id, e.kind, e.title, e.body, e.proposal_id FROM (
      SELECT event_id, 'threat' AS kind, title, body, proposal_id FROM threat_events
       WHERE status = 'active' AND starts_at <= now() AND expires_at > now()
         AND proposal_id IS NOT NULL
      UNION ALL
      SELECT event_id, 'protection', title, body, proposal_id FROM protection_events
       WHERE status = 'active' AND starts_at <= now() AND review_by > now()
    ) e
     WHERE (cardinality(%(ids)s::uuid[]) = 0 OR e.event_id = ANY(%(ids)s::uuid[]))
       AND NOT EXISTS (SELECT 1 FROM intel_lessons l
                        WHERE l.event_id = e.event_id AND l.retired_at IS NULL)
       AND NOT EXISTS (SELECT 1 FROM intel_proposals p
                        WHERE p.kind = 'lesson' AND p.status = 'pending'
                          AND p.target->>'event_id' = e.event_id::text)
     ORDER BY e.event_id
     LIMIT %(limit)s
"""


@dataclass(frozen=True)
class DraftEvent:
    """A live event a lesson_draft run may draft for, with its proposal's active evidence."""

    event_id: UUID
    kind: Literal["threat", "protection"]
    title: str
    body: str
    signals: tuple[ContextSignal, ...]


class LessonStore(Protocol):
    async def queue_draft(self, *, event_ids: Sequence[UUID], operator: str) -> UUID: ...
    async def list_lessons(
        self, *, live: bool, limit: int, cursor: tuple[datetime, UUID] | None
    ) -> list[dict[str, Any]]: ...
    async def retire(self, lesson_id: UUID, *, operator: str, reason: str) -> RetireOutcome: ...
    async def draft_events(self, event_ids: Sequence[UUID], *, limit: int) -> list[DraftEvent]: ...


class PostgresLessonStore:
    """The lesson library, retirement, and lesson_draft runs (spec 2026-10-15 §5)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def queue_draft(self, *, event_ids: Sequence[UUID], operator: str) -> UUID:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO intel_runs (kind, request, requested_by)"
                " VALUES ('lesson_draft', %s, %s) RETURNING run_id",
                (Jsonb({"event_ids": [str(e) for e in event_ids]}), operator),
            )
            row = await cur.fetchone()
        assert row is not None
        run_id: UUID = row[0]
        return run_id

    async def list_lessons(
        self, *, live: bool, limit: int, cursor: tuple[datetime, UUID] | None
    ) -> list[dict[str, Any]]:
        where = "retired_at IS NULL" if live else "retired_at IS NOT NULL"
        after = " AND (approved_at, lesson_id) < (%(at)s, %(id)s)" if cursor is not None else ""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params.update(at=cursor[0], id=cursor[1])
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_LESSON_COLUMNS} FROM intel_lessons WHERE {where}{after}"
                " ORDER BY approved_at DESC, lesson_id DESC LIMIT %(limit)s",
                params,
            )
            return await cur.fetchall()

    async def retire(self, lesson_id: UUID, *, operator: str, reason: str) -> RetireOutcome:
        """Retire a live lesson, audited in the same transaction; it leaves svc.v_lessons at once.
        A lesson already retired answers already_retired, an unknown id not_found."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE intel_lessons SET retired_by = %s, retired_at = now(), retire_reason = %s"
                " WHERE lesson_id = %s AND retired_at IS NULL RETURNING 1",
                (operator, reason, lesson_id),
            )
            if await cur.fetchone() is not None:
                await conn.execute(
                    "INSERT INTO audit_log (actor_type, action, resource_id, metadata)"
                    " VALUES ('operator', 'intel.lesson_retired', %s, %s)",
                    (lesson_id, Jsonb({"operator": operator, "reason": reason})),
                )
                return "retired"
            cur = await conn.execute(
                "SELECT 1 FROM intel_lessons WHERE lesson_id = %s", (lesson_id,)
            )
            return "already_retired" if await cur.fetchone() is not None else "not_found"

    async def draft_events(self, event_ids: Sequence[UUID], *, limit: int) -> list[DraftEvent]:
        from imageshield.intel.proposal_store import fetch_linked_signals

        async with self._pool.connection() as conn:
            cur = await conn.execute(_DRAFT_EVENTS_SQL, {"ids": list(event_ids), "limit": limit})
            rows = await cur.fetchall()
            linked = await fetch_linked_signals(conn, [row[4] for row in rows])
        return [
            DraftEvent(
                event_id=row[0],
                kind=row[1],
                title=row[2],
                body=row[3] or "",
                signals=tuple(s for s in linked[row[4]] if s.status == "active"),
            )
            for row in rows
        ]
