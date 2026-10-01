"""intel_proposals: generation writes, their supersession, and the two reads (spec §3.6,
§4.3, §4.7).

Three modules write this table, so that "which code can approve" is answerable by file:
- this one writes NEW proposals and generation-time supersession;
- ``decisions.py`` decides, is the only writer of 'approved', and inserts the threat event an
  approval creates;
- ``reconcile.py`` retargets and supersedes pending rows when the quiz moves.

Statuses are SQL LITERALS here, never parameters: tests/test_boundaries.py relies on it.
Nothing DELETEs, because intel_rw holds no DELETE grant (0039).

This module also reads threat_events and protection_events, with its own SQL (the threat store is
not importable from intel/, spec §6.1): the live events for the prompt, and each event proposal's
related_events, which are the live events of its own direction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.approvable import EVENT_KINDS, read_flags
from imageshield.intel.bounds import PROPOSAL_CONTEXT_MAX_EVENTS
from imageshield.intel.models import Vocabulary
from imageshield.intel.proposal_models import (
    Attachment,
    ContextSignal,
    LiveEvent,
    LiveProtection,
    NewProposal,
    PendingEvent,
    ProposalRecord,
    WriteResult,
)
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject, parse_vocabulary

log = structlog.get_logger("imageshield.intel")

ALL_KINDS: tuple[str, ...] = (
    "weight_change",
    "threat_event",
    "protection_event",
    "weight_suggestion",
    "coverage_gap",
)

PROPOSAL_COLUMNS = """proposal_id, kind, status, supersede_reason, target, suggested, decided,
    rationale, against_scoring_version, against_release_no, run_id, model_id, prompt_version,
    decided_by, decided_at, decision_reason, applied_ref, created_at"""

# document_key: the document's canonical URL hash. Duplicate detection compares it, so a page a
# later run reads again (a new intel_documents row) is the same document (spec §4.3).
_CONTEXT_COLUMNS = """s.signal_id, s.category, s.direction, s.tags, s.unregistered_subjects,
    s.summary, d.trust, d.publisher_domain, s.status, s.created_at, d.url_hash AS document_key"""
_CONTEXT_FROM = "intel_signals s JOIN intel_documents d ON d.document_id = s.document_id"

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_INSERT_SQL = """
    INSERT INTO intel_proposals (kind, status, target, suggested, rationale,
        against_scoring_version, against_release_no, run_id, model_id, prompt_version)
    VALUES (%(kind)s, 'pending', %(target)s, %(suggested)s, %(rationale)s, %(asv)s, %(arn)s,
            %(run_id)s, %(model_id)s, %(prompt_version)s)
    RETURNING proposal_id
"""

_SUPERSEDE_CELL_SQL = """
    UPDATE intel_proposals SET status = 'superseded', supersede_reason = 'newer_proposal'
     WHERE kind = 'weight_change' AND status = 'pending'
       AND target->>'question_key' = %s AND target->>'option' = %s
    RETURNING proposal_id
"""

_SUPERSEDE_IDS_SQL = """
    UPDATE intel_proposals SET status = 'superseded', supersede_reason = 'newer_proposal'
     WHERE proposal_id = ANY(%s::uuid[]) AND status = 'pending'
    RETURNING proposal_id
"""

# The threat half of svc.v_active_scoped_events, read from the base table: intel_rw has no
# USAGE on svc. An approved event's evidence is its proposal's linked signals.
_LIVE_EVENTS_SQL = """
    SELECT e.event_id, e.kind, e.title, e.severity, e.tags, e.is_global, e.starts_at,
           e.expires_at, e.proposal_id,
           coalesce(array_agg(ps.signal_id ORDER BY ps.signal_id)
                    FILTER (WHERE ps.signal_id IS NOT NULL), '{}') AS signal_ids
      FROM threat_events e
      LEFT JOIN intel_proposal_signals ps ON ps.proposal_id = e.proposal_id
     WHERE e.status = 'active' AND e.starts_at <= now() AND e.expires_at > now()
       AND e.tags && %(tags)s::text[]
     GROUP BY e.event_id
     ORDER BY e.created_at DESC, e.event_id DESC
     LIMIT %(limit)s
"""

# Over ACTIVE signals only (spec §3.5: a retracted signal is excluded everywhere). A proposal
# whose every signal was retracted is no duplicate candidate and no attach target, so a page
# read again opens a fresh proposal written from the new evidence rather than reviving it
# (final review M1, 2026-10-01). ``categories`` feeds the attach floor (M2).
_PENDING_EVENTS_SQL = """
    SELECT p.proposal_id, p.kind, p.target, p.suggested,
           array_agg(ps.signal_id ORDER BY ps.signal_id)
             FILTER (WHERE s.status = 'active') AS signal_ids,
           array_agg(DISTINCT d.url_hash) FILTER (WHERE s.status = 'active') AS document_keys,
           array_agg(DISTINCT s.category) FILTER (WHERE s.status = 'active') AS categories
      FROM intel_proposals p
      JOIN intel_proposal_signals ps ON ps.proposal_id = p.proposal_id
      JOIN intel_signals s ON s.signal_id = ps.signal_id
      JOIN intel_documents d ON d.document_id = s.document_id
     WHERE p.kind = ANY(%(kinds)s::text[]) AND p.status = 'pending'
       AND ARRAY(SELECT jsonb_array_elements_text(p.target -> 'tags')) && %(tags)s::text[]
     GROUP BY p.proposal_id
    HAVING bool_or(s.status = 'active')
     ORDER BY p.created_at DESC, p.proposal_id DESC
     LIMIT %(limit)s
"""

# The protection half of svc.v_active_scoped_events, read from the base table (intel_rw has no
# USAGE on svc), plus every live GLOBAL credit: a global credit overlaps every proposal. A
# credit's evidence is its approving proposal's linked signals.
_LIVE_PROTECTIONS_SQL = """
    SELECT e.event_id, e.title, e.strength, e.tags, e.is_global, e.starts_at, e.review_by,
           e.proposal_id, e.renews_event_id,
           coalesce(array_agg(ps.signal_id ORDER BY ps.signal_id)
                    FILTER (WHERE ps.signal_id IS NOT NULL), '{}') AS signal_ids
      FROM protection_events e
      LEFT JOIN intel_proposal_signals ps ON ps.proposal_id = e.proposal_id
     WHERE e.status = 'active' AND e.starts_at <= now() AND e.review_by > now()
       AND (e.is_global OR e.tags && %(tags)s::text[])
     GROUP BY e.event_id
     ORDER BY e.created_at DESC, e.event_id DESC
     LIMIT %(limit)s
"""


# A credit a renewal may still continue: active and not yet at its review date. The decision
# locks the credit with it (intel/decisions.py) and the reads ask it (``ended_renewals``), so
# the panel and the decision cannot disagree about a late or orphaned renewal (final review
# M3/M4, 2026-10-01).
LIVE_CREDIT_SQL = "status = 'active' AND review_by > now()"


def _context(row: dict[str, Any]) -> ContextSignal:
    return ContextSignal(
        signal_id=row["signal_id"],
        category=row["category"],
        direction=row["direction"],
        tags=tuple(row["tags"]),
        unregistered_subjects=tuple(row["unregistered_subjects"]),
        summary=row["summary"],
        trust=row["trust"],
        publisher_domain=row["publisher_domain"],
        status=row["status"],
        created_at=row["created_at"],
        document_key=row["document_key"],
    )


def record_of(row: dict[str, Any]) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        kind=row["kind"],
        status=row["status"],
        target=row["target"],
        suggested=row["suggested"],
        decided=row["decided"],
        against_release_no=row["against_release_no"],
        created_at=row["created_at"],
    )


def _live_event(row: dict[str, Any]) -> LiveEvent:
    return LiveEvent(
        event_id=row["event_id"],
        kind=row["kind"],
        title=row["title"],
        severity=row["severity"],
        tags=tuple(row["tags"]),
        is_global=row["is_global"],
        starts_at=row["starts_at"],
        expires_at=row["expires_at"],
        proposal_id=row["proposal_id"],
        signal_ids=tuple(row["signal_ids"]),
    )


def _pending_event(row: dict[str, Any]) -> PendingEvent:
    tags = row["target"].get("tags") or []
    severity = row["suggested"].get("severity")
    return PendingEvent(
        proposal_id=row["proposal_id"],
        kind=row["kind"],
        tags=tuple(str(t) for t in tags),
        title=str(row["suggested"].get("title", "")),
        severity=severity if isinstance(severity, int) and not isinstance(severity, bool) else None,
        signal_ids=tuple(row["signal_ids"]),
        document_keys=frozenset(row["document_keys"]),
        categories=frozenset(row["categories"]),
    )


def _live_protection(row: dict[str, Any]) -> LiveProtection:
    return LiveProtection(
        event_id=row["event_id"],
        title=row["title"],
        strength=row["strength"],
        tags=tuple(row["tags"]),
        is_global=row["is_global"],
        starts_at=row["starts_at"],
        review_by=row["review_by"],
        proposal_id=row["proposal_id"],
        renews_event_id=row["renews_event_id"],
        signal_ids=tuple(row["signal_ids"]),
    )


async def load_scoring_vocabulary(conn: AsyncConnection[Any]) -> ScoringVocabulary | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT release_no, map_version, scoring_version, quiz_version, document"
        " FROM intel_vocabulary WHERE id = 1"
    )
    row = await cur.fetchone()
    return parse_vocabulary(Vocabulary.model_validate(row)) if row is not None else None


async def fetch_linked_signals(
    conn: AsyncConnection[Any], proposal_ids: Sequence[UUID]
) -> dict[UUID, list[ContextSignal]]:
    """Every signal linked to each proposal, retracted ones included (the predicates filter)."""
    linked: dict[UUID, list[ContextSignal]] = {pid: [] for pid in proposal_ids}
    if not proposal_ids:
        return linked
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT ps.proposal_id AS linked_to, {_CONTEXT_COLUMNS}"
        f" FROM intel_proposal_signals ps JOIN {_CONTEXT_FROM} ON s.signal_id = ps.signal_id"
        " WHERE ps.proposal_id = ANY(%s::uuid[]) ORDER BY s.created_at, s.signal_id",
        (list(proposal_ids),),
    )
    for row in await cur.fetchall():
        linked[row["linked_to"]].append(_context(row))
    return linked


async def ended_renewals(
    conn: AsyncConnection[Any], rows: Sequence[dict[str, Any]]
) -> frozenset[UUID]:
    """The PENDING renewal proposals among ``rows`` whose credit is no longer live (retracted,
    or past its review date): ``why_not = renewed_credit_ended``. Only pending ones, since an
    applied renewal's predecessor lapses by design the moment the renewal takes over."""
    renews: dict[UUID, UUID] = {}
    for row in rows:
        if row["kind"] != "protection_event" or row["status"] != "pending":
            continue
        raw = (row["target"] or {}).get("renews_event_id")
        if not isinstance(raw, str):
            continue
        try:
            renews[row["proposal_id"]] = UUID(raw)
        except ValueError:
            continue
    if not renews:
        return frozenset()
    cur = await conn.execute(
        f"SELECT event_id FROM protection_events WHERE event_id = ANY(%s::uuid[])"
        f" AND {LIVE_CREDIT_SQL}",
        (sorted(set(renews.values())),),
    )
    live = {r[0] for r in await cur.fetchall()}
    return frozenset(pid for pid, event in renews.items() if event not in live)


async def live_threat_events(
    conn: AsyncConnection[Any], *, tags: Sequence[str], limit: int
) -> list[LiveEvent]:
    """Active threat events carrying a tag in ``tags``, newest first, bounded. Empty ``tags``
    overlaps nothing."""
    if not tags:
        return []
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(_LIVE_EVENTS_SQL, {"tags": list(tags), "limit": limit})
    return [_live_event(row) for row in await cur.fetchall()]


async def live_protection_events(
    conn: AsyncConnection[Any], *, tags: Sequence[str], limit: int
) -> list[LiveProtection]:
    """Live protection credits carrying a tag in ``tags``, plus every live global credit,
    newest first, bounded. Empty ``tags`` still finds the global ones."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(_LIVE_PROTECTIONS_SQL, {"tags": list(tags), "limit": limit})
    return [_live_protection(row) for row in await cur.fetchall()]


async def active_subject_signals(
    conn: AsyncConnection[Any], *, since: datetime, limit: int
) -> list[ContextSignal]:
    """Active signals of the window that name an unregistered subject, newest first: the pool a
    resolved gap's regeneration draws matching evidence from (spec §4.9)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
        " WHERE s.status = 'active' AND s.created_at >= %(since)s"
        " AND cardinality(s.unregistered_subjects) > 0"
        " ORDER BY s.created_at DESC, s.signal_id DESC LIMIT %(limit)s",
        {"since": since, "limit": limit},
    )
    return [_context(row) for row in await cur.fetchall()]


class ProposalStore(Protocol):
    async def proposals_written(self, run_id: UUID) -> bool: ...
    async def run_signals(self, run_id: UUID) -> list[ContextSignal]: ...
    async def related_signals(
        self,
        *,
        exclude: Sequence[UUID],
        tags: Sequence[str],
        categories: Sequence[str],
        since: datetime,
        limit: int,
    ) -> list[ContextSignal]: ...
    async def gap_candidates(
        self, *, since: datetime, unmapped_tags: Sequence[str], limit: int
    ) -> list[ContextSignal]: ...
    async def signals_by_id(self, signal_ids: Sequence[UUID]) -> list[ContextSignal]: ...
    async def pending_event_proposals(
        self, *, tags: Sequence[str], limit: int
    ) -> list[PendingEvent]: ...
    async def active_threat_events(self, *, tags: Sequence[str], limit: int) -> list[LiveEvent]: ...

    async def active_protection_events(
        self, *, tags: Sequence[str], limit: int
    ) -> list[LiveProtection]: ...
    async def write_generated(
        self,
        run_id: UUID,
        proposals: Sequence[NewProposal],
        *,
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
        attachments: Sequence[Attachment] = (),
    ) -> WriteResult | None: ...
    async def list_proposals(
        self,
        *,
        statuses: Sequence[str] | None,
        kinds: Sequence[str] | None,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...
    async def get_proposal(self, proposal_id: UUID) -> dict[str, Any] | None: ...


class PostgresProposalStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def proposals_written(self, run_id: UUID) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT proposals_written_at IS NOT NULL FROM intel_runs WHERE run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
        return bool(row and row[0])

    async def run_signals(self, run_id: UUID) -> list[ContextSignal]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
                " WHERE d.run_id = %s AND s.status = 'active'"
                " ORDER BY s.created_at, s.signal_id",
                (run_id,),
            )
            return [_context(row) for row in await cur.fetchall()]

    async def related_signals(
        self,
        *,
        exclude: Sequence[UUID],
        tags: Sequence[str],
        categories: Sequence[str],
        since: datetime,
        limit: int,
    ) -> list[ContextSignal]:
        """spec §4.3: active signals from the window with overlapping tags or category, newest
        first, bounded."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
                " WHERE s.status = 'active' AND s.created_at >= %(since)s"
                " AND NOT (s.signal_id = ANY(%(exclude)s::uuid[]))"
                " AND (s.tags && %(tags)s::text[] OR s.category = ANY(%(categories)s::text[]))"
                " ORDER BY s.created_at DESC, s.signal_id DESC LIMIT %(limit)s",
                {
                    "since": since,
                    "exclude": list(exclude),
                    "tags": list(tags),
                    "categories": list(categories),
                    "limit": limit,
                },
            )
            return [_context(row) for row in await cur.fetchall()]

    async def gap_candidates(
        self, *, since: datetime, unmapped_tags: Sequence[str], limit: int
    ) -> list[ContextSignal]:
        """Every active signal in the window that could concern an uncovered subject: one
        that names an unregistered subject, or carries an unmapped tag. The gap validator
        (intel/generation.py) decides which of these concern a given subject."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
                " WHERE s.status = 'active' AND s.created_at >= %(since)s"
                " AND (cardinality(s.unregistered_subjects) > 0"
                "      OR s.tags && %(unmapped)s::text[])"
                " ORDER BY s.created_at DESC, s.signal_id DESC LIMIT %(limit)s",
                {"since": since, "unmapped": list(unmapped_tags), "limit": limit},
            )
            return [_context(row) for row in await cur.fetchall()]

    async def signals_by_id(self, signal_ids: Sequence[UUID]) -> list[ContextSignal]:
        """The ACTIVE signals among ``signal_ids``: a gap_regenerate run's input (spec §4.9). A
        signal retracted since the run was queued is left out, as everywhere else."""
        if not signal_ids:
            return []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
                " WHERE s.signal_id = ANY(%s::uuid[]) AND s.status = 'active'"
                " ORDER BY s.created_at, s.signal_id",
                (list(signal_ids),),
            )
            return [_context(row) for row in await cur.fetchall()]

    async def pending_event_proposals(
        self, *, tags: Sequence[str], limit: int
    ) -> list[PendingEvent]:
        """Pending event proposals whose tags overlap ``tags``, newest first, bounded, each with
        its ACTIVE linked signals, their documents and categories (spec §4.3: prompt context,
        attach targets and duplicate candidates). One whose every signal was retracted is left
        out (§3.5)."""
        if not tags:
            return []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                _PENDING_EVENTS_SQL,
                {"kinds": sorted(EVENT_KINDS), "tags": list(tags), "limit": limit},
            )
            return [_pending_event(row) for row in await cur.fetchall()]

    async def active_threat_events(self, *, tags: Sequence[str], limit: int) -> list[LiveEvent]:
        async with self._pool.connection() as conn:
            return await live_threat_events(conn, tags=tags, limit=limit)

    async def active_protection_events(
        self, *, tags: Sequence[str], limit: int
    ) -> list[LiveProtection]:
        async with self._pool.connection() as conn:
            return await live_protection_events(conn, tags=tags, limit=limit)

    async def write_generated(
        self,
        run_id: UUID,
        proposals: Sequence[NewProposal],
        *,
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
        attachments: Sequence[Attachment] = (),
    ) -> WriteResult | None:
        """All of a run's proposals and their supersession, in ONE transaction with
        ``proposals_written_at``. Returns None when that is already set: a reclaimed run never
        generates twice (spec §4.3). An empty ``proposals`` still sets it, which is how a
        consumed model verdict (refusal, max_tokens) is recorded."""
        written: list[UUID] = []
        superseded: list[UUID] = []
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE intel_runs SET proposals_written_at = now()"
                " WHERE run_id = %s AND proposals_written_at IS NULL RETURNING 1",
                (run_id,),
            )
            if await cur.fetchone() is None:
                return None
            pending_gaps: list[tuple[UUID, dict[str, Any]]] | None = None
            for proposal in proposals:
                # A weight change and a coverage gap supersede their pending twin. An event
                # proposal of either kind supersedes nothing: a repeat of a pending one was
                # made an attachment before it got here (spec §4.3), so what arrives is new.
                if proposal.kind == "weight_change":
                    cur = await conn.execute(
                        _SUPERSEDE_CELL_SQL,
                        (proposal.target["question_key"], proposal.target["option"]),
                    )
                    superseded += [r[0] for r in await cur.fetchall()]
                elif proposal.kind == "coverage_gap":
                    if pending_gaps is None:
                        cur = await conn.execute(
                            "SELECT proposal_id, target FROM intel_proposals"
                            " WHERE kind = 'coverage_gap' AND status = 'pending' FOR UPDATE"
                        )
                        pending_gaps = [(r[0], r[1]) for r in await cur.fetchall()]
                    key = normalise_subject(str(proposal.target["subject"]))
                    older = [
                        pid
                        for pid, target in pending_gaps
                        if normalise_subject(str(target.get("subject", ""))) == key
                    ]
                    if older:
                        cur = await conn.execute(_SUPERSEDE_IDS_SQL, (older,))
                        superseded += [r[0] for r in await cur.fetchall()]
                weight = proposal.kind == "weight_change"
                cur = await conn.execute(
                    _INSERT_SQL,
                    {
                        "kind": proposal.kind,
                        "target": Jsonb(proposal.target),
                        "suggested": Jsonb(proposal.suggested),
                        "rationale": proposal.rationale,
                        "asv": against_scoring_version if weight else None,
                        "arn": against_release_no if weight else None,
                        "run_id": run_id,
                        "model_id": model_id,
                        "prompt_version": prompt_version,
                    },
                )
                row = await cur.fetchone()
                assert row is not None
                proposal_id: UUID = row[0]
                await conn.execute(
                    "INSERT INTO intel_proposal_signals (proposal_id, signal_id)"
                    " SELECT %s, unnest(%s::uuid[])",
                    (proposal_id, list(proposal.signal_ids)),
                )
                written.append(proposal_id)
            attached: list[UUID] = []
            attach_dropped = 0
            if attachments:
                # spec §4.3, re-checked under lock: the target must STILL be a pending event
                # proposal. A decision may have dismissed it since the model call.
                cur = await conn.execute(
                    "SELECT proposal_id FROM intel_proposals"
                    " WHERE proposal_id = ANY(%s::uuid[]) AND status = 'pending'"
                    " AND kind = ANY(%s::text[]) FOR UPDATE",
                    ([a.proposal_id for a in attachments], sorted(EVENT_KINDS)),
                )
                still_open = {r[0] for r in await cur.fetchall()}
                for attachment in attachments:
                    if attachment.proposal_id not in still_open:
                        attach_dropped += 1
                        continue
                    await conn.execute(
                        "INSERT INTO intel_proposal_signals (proposal_id, signal_id)"
                        " SELECT %s, unnest(%s::uuid[]) ON CONFLICT DO NOTHING",
                        (attachment.proposal_id, list(attachment.signal_ids)),
                    )
                    if attachment.proposal_id not in attached:
                        attached.append(attachment.proposal_id)
            if written or superseded or attached:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.proposals_written",
                        "resource_id": run_id,
                        "metadata": Jsonb(
                            {
                                "written": [str(i) for i in written],
                                "superseded": [str(i) for i in superseded],
                                "attached": [str(i) for i in attached],
                            }
                        ),
                    },
                )
        return WriteResult(tuple(written), tuple(superseded), tuple(attached), attach_dropped)

    async def list_proposals(
        self,
        *,
        statuses: Sequence[str] | None,
        kinds: Sequence[str] | None,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Keyset-paged, newest first. Omitting ``kinds`` omits weight_suggestion (spec §3.6:
        delivered advice, never in the review queue)."""
        clauses = ["kind = ANY(%(kinds)s::text[])"]
        params: dict[str, Any] = {
            "kinds": list(kinds) if kinds else [k for k in ALL_KINDS if k != "weight_suggestion"],
            "limit": limit,
        }
        if statuses:
            clauses.append("status = ANY(%(statuses)s::text[])")
            params["statuses"] = list(statuses)
        if cursor is not None:
            clauses.append("(created_at, proposal_id) < (%(at)s, %(id)s)")
            params.update(at=cursor[0], id=cursor[1])
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {PROPOSAL_COLUMNS} FROM intel_proposals WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC, proposal_id DESC LIMIT %(limit)s",
                params,
            )
            rows = await cur.fetchall()
            vocabulary = await load_scoring_vocabulary(conn)
            linked = await fetch_linked_signals(conn, [r["proposal_id"] for r in rows])
            ended = await ended_renewals(conn, rows)
        return [
            _annotated(row, linked[row["proposal_id"]], vocabulary, row["proposal_id"] in ended)
            for row in rows
        ]

    async def get_proposal(self, proposal_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {PROPOSAL_COLUMNS} FROM intel_proposals WHERE proposal_id = %s",
                (proposal_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            vocabulary = await load_scoring_vocabulary(conn)
            linked = (await fetch_linked_signals(conn, [proposal_id]))[proposal_id]
            ended = await ended_renewals(conn, [row])
            signal_ids = [s.signal_id for s in linked]
            await cur.execute(
                "SELECT signal_id, document_id, category, direction, tags, unregistered_subjects,"
                " summary, status, retracted_at, retract_reason, created_at FROM intel_signals"
                " WHERE signal_id = ANY(%s::uuid[]) ORDER BY created_at, signal_id",
                (signal_ids,),
            )
            signals = await cur.fetchall()
            await cur.execute(
                "SELECT signal_id, excerpt_id, quote_text, char_start, char_end, quote_sha256"
                " FROM intel_excerpts WHERE signal_id = ANY(%s::uuid[]) ORDER BY char_start",
                (signal_ids,),
            )
            excerpts = await cur.fetchall()
            await cur.execute(
                "SELECT document_id, document_url, final_url, publisher_domain, trust, title,"
                " published_at, fetched_at FROM intel_documents"
                " WHERE document_id = ANY(%s::uuid[])",
                ([s["document_id"] for s in signals],),
            )
            documents = {d["document_id"]: d for d in await cur.fetchall()}
            related: list[dict[str, Any]] = []
            own_tags = [t for t in (row["target"].get("tags") or []) if isinstance(t, str)]
            # The live events of the proposal's OWN direction: what it could duplicate. Never
            # the proposal's own event.
            if row["kind"] == "threat_event":
                related = [
                    event.related()
                    for event in await live_threat_events(
                        conn, tags=own_tags, limit=PROPOSAL_CONTEXT_MAX_EVENTS
                    )
                    if event.proposal_id != proposal_id
                ]
            elif row["kind"] == "protection_event":
                related = [
                    credit.related()
                    for credit in await live_protection_events(
                        conn, tags=own_tags, limit=PROPOSAL_CONTEXT_MAX_EVENTS
                    )
                    if credit.proposal_id != proposal_id
                ]
        by_signal: dict[UUID, list[dict[str, Any]]] = {}
        for excerpt in excerpts:
            by_signal.setdefault(excerpt.pop("signal_id"), []).append(excerpt)
        return {
            **_annotated(row, linked, vocabulary, proposal_id in ended),
            "related_events": related,
            "signals": [
                {
                    **{k: v for k, v in s.items() if k != "document_id"},
                    "tags": list(s["tags"]),
                    "unregistered_subjects": list(s["unregistered_subjects"]),
                    "excerpts": by_signal.get(s["signal_id"], []),
                    "document": documents.get(s["document_id"]),
                }
                for s in signals
            ],
        }


def _annotated(
    row: dict[str, Any],
    linked: list[ContextSignal],
    vocabulary: ScoringVocabulary | None,
    renewed_credit_ended: bool = False,
) -> dict[str, Any]:
    return {
        **row,
        "signal_ids": [s.signal_id for s in linked],
        **read_flags(
            record_of(row), linked, vocabulary, renewed_credit_ended=renewed_credit_ended
        ),
    }
