"""intel_proposals: generation writes, their supersession, and the two reads (spec §3.6,
§4.3, §4.7).

Three modules write this table, so that "which code can approve" is answerable by file:
- this one writes NEW proposals and generation-time supersession;
- ``decisions.py`` decides, and is the only writer of 'approved';
- ``reconcile.py`` retargets and supersedes pending rows when the quiz moves.

Statuses are SQL LITERALS here, never parameters: tests/test_boundaries.py relies on it.
Nothing DELETEs, because intel_rw holds no DELETE grant (0039).
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

from imageshield.intel.approvable import read_flags
from imageshield.intel.models import Vocabulary
from imageshield.intel.proposal_models import (
    ContextSignal,
    NewProposal,
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

_CONTEXT_COLUMNS = """s.signal_id, s.category, s.direction, s.tags, s.unregistered_subjects,
    s.summary, d.trust, d.publisher_domain, s.status, s.created_at"""
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
    async def write_generated(
        self,
        run_id: UUID,
        proposals: Sequence[NewProposal],
        *,
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
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

    async def write_generated(
        self,
        run_id: UUID,
        proposals: Sequence[NewProposal],
        *,
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
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
                if proposal.kind == "weight_change":
                    cur = await conn.execute(
                        _SUPERSEDE_CELL_SQL,
                        (proposal.target["question_key"], proposal.target["option"]),
                    )
                    superseded += [r[0] for r in await cur.fetchall()]
                else:
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
            if written or superseded:
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
                            }
                        ),
                    },
                )
        return WriteResult(tuple(written), tuple(superseded))

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
        return [_annotated(row, linked[row["proposal_id"]], vocabulary) for row in rows]

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
        by_signal: dict[UUID, list[dict[str, Any]]] = {}
        for excerpt in excerpts:
            by_signal.setdefault(excerpt.pop("signal_id"), []).append(excerpt)
        return {
            **_annotated(row, linked, vocabulary),
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
    row: dict[str, Any], linked: list[ContextSignal], vocabulary: ScoringVocabulary | None
) -> dict[str, Any]:
    return {
        **row,
        "signal_ids": [s.signal_id for s in linked],
        **read_flags(record_of(row), linked, vocabulary),
    }
