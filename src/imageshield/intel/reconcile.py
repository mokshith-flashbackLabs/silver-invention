"""The quiz-change reconcile (spec §4.9). Intel holds no copy of the quiz that can go stale
except the vocabulary, and it reacts to every new vocabulary on its own: within one poll of a
push, the pending weight changes match the live quiz, and nothing an operator approved has been
silently rewritten.

Deterministic and idempotent; no model runs here. ``plan_reconcile`` is pure, and
``PostgresReconciler`` applies its plan and records the reconciled pair in ONE transaction.

``reconcile`` handles weight_change rows, once per new pair. ``resolve_gaps`` (step 3) is the
"tag newly mapped" row, and it is STATE-BASED: it runs on every tick and looks at what is mapped
now, because pairs reconciled before it shipped are already recorded here. A pending gap the live
quiz now covers is superseded resolved_by_quiz in the same transaction that queues the
gap_regenerate run re-proposing its evidence; a regeneration the gate refused is queued again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from imageshield.intel.bounds import (
    COVERAGE_GAP_POOL_MAX,
    COVERAGE_GAP_WINDOW_DAYS,
    GAP_REGENERATE_MAX_RUNS,
    GAP_REGENERATE_RETRY_HOURS,
    PROPOSAL_CONTEXT_MAX_SIGNALS,
)
from imageshield.intel.models import Vocabulary
from imageshield.intel.proposal_models import (
    ContextSignal,
    CoverageGapTarget,
    GapPass,
    ReconcileResult,
    SupersedeReason,
    WeightChangeTarget,
)
from imageshield.intel.proposal_store import active_subject_signals, load_scoring_vocabulary
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject, parse_vocabulary

log = structlog.get_logger("imageshield.intel")

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_LOCK_PENDING_GAPS_SQL = (
    "SELECT proposal_id FROM intel_proposals"
    " WHERE kind = 'coverage_gap' AND status = 'pending' FOR UPDATE"
)

_PENDING_GAPS_SQL = """
    SELECT p.proposal_id, p.target, p.created_at,
           coalesce(array_agg(s.signal_id ORDER BY s.created_at, s.signal_id)
                    FILTER (WHERE s.status = 'active'), '{}') AS signal_ids
      FROM intel_proposals p
      LEFT JOIN intel_proposal_signals ps ON ps.proposal_id = p.proposal_id
      LEFT JOIN intel_signals s ON s.signal_id = ps.signal_id
     WHERE p.proposal_id = ANY(%s::uuid[])
     GROUP BY p.proposal_id
"""

_QUEUE_REGENERATION_SQL = """
    INSERT INTO intel_runs (kind, request, requested_by)
    VALUES ('gap_regenerate', %s, 'schedule')
    RETURNING run_id
"""

_RESOLVE_GAP_SQL = """
    UPDATE intel_proposals
       SET status = 'superseded', supersede_reason = 'resolved_by_quiz',
           target = target || jsonb_build_object('regenerated_by_run_id', %s::text)
     WHERE proposal_id = %s AND status = 'pending'
"""

_FINISHED_REGENERATIONS_SQL = """
    SELECT p.proposal_id, r.run_id, r.request, r.completed_at,
           (SELECT count(*) FROM intel_runs x
             WHERE x.kind = 'gap_regenerate'
               AND x.request ->> 'coverage_gap_id' = p.proposal_id::text) AS runs
      FROM intel_proposals p
      JOIN intel_runs r ON r.run_id::text = p.target ->> 'regenerated_by_run_id'
     WHERE p.kind = 'coverage_gap' AND p.status = 'superseded'
       AND p.supersede_reason = 'resolved_by_quiz'
       AND r.status IN ('refused', 'failed') AND r.proposals_written_at IS NULL
       AND r.completed_at IS NOT NULL
"""

_FOLLOW_RETRY_SQL = """
    UPDATE intel_proposals
       SET target = jsonb_set(target, '{regenerated_by_run_id}', to_jsonb(%s::text))
     WHERE proposal_id = %s AND status = 'superseded'
       AND target ->> 'regenerated_by_run_id' = %s
"""


async def _queue_regeneration(conn: AsyncConnection[Any], request: dict[str, Any]) -> UUID:
    cur = await conn.execute(_QUEUE_REGENERATION_SQL, (Jsonb(request),))
    row = await cur.fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


@dataclass(frozen=True)
class PendingWeightChange:
    proposal_id: UUID
    question_key: str
    option: str
    current: int
    against_release_no: int
    created_at: datetime


@dataclass(frozen=True)
class ReconcilePlan:
    retarget: tuple[tuple[UUID, str, str], ...]
    supersede: tuple[tuple[UUID, SupersedeReason], ...]

    @property
    def empty(self) -> bool:
        return not self.retarget and not self.supersede


def plan_reconcile(
    pending: Sequence[PendingWeightChange],
    vocabulary: ScoringVocabulary,
    *,
    reconciled_release_no: int | None,
) -> ReconcilePlan:
    """spec §4.9 for pending weight changes.

    The renames a proposal needs are those above BOTH the last reconciled release and the
    release it was generated against. The single worker runs this between runs, so a run's
    proposals were generated against a vocabulary no later than the next reconcile sees."""
    supersede: list[tuple[UUID, SupersedeReason]] = []
    finals: list[tuple[PendingWeightChange, str]] = []
    for p in sorted(pending, key=lambda p: (p.created_at, str(p.proposal_id))):
        threshold = p.against_release_no
        if reconciled_release_no is not None:
            threshold = max(threshold, reconciled_release_no)
        final = vocabulary.fold_renames(p.question_key, p.option, after_release_no=threshold)
        question = vocabulary.question(p.question_key)
        live = vocabulary.deduction(p.question_key, final)
        if question is None or not question.mutable or live is None or live != p.current:
            supersede.append((p.proposal_id, "cell_changed"))
            continue
        finals.append((p, final))
    newest: dict[tuple[str, str], UUID] = {}
    for p, final in finals:  # ascending created_at: a later proposal replaces an earlier one
        cell = (p.question_key, final)
        if cell in newest:
            supersede.append((newest[cell], "newer_proposal"))
        newest[cell] = p.proposal_id
    retarget = tuple(
        (p.proposal_id, p.option, final)
        for p, final in finals
        if newest[(p.question_key, final)] == p.proposal_id and final != p.option
    )
    return ReconcilePlan(retarget=retarget, supersede=tuple(supersede))


@dataclass(frozen=True)
class PendingGap:
    proposal_id: UUID
    subject: str
    suggested_slug: str | None
    signal_ids: tuple[UUID, ...]  # its linked ACTIVE signals, oldest first
    created_at: datetime


@dataclass(frozen=True)
class GapResolution:
    proposal_id: UUID
    tag: str
    signal_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class FinishedRegeneration:
    """A superseded gap whose newest regeneration ended refused or failed without writing
    proposals. ``runs`` is every gap_regenerate run queued for this gap so far."""

    proposal_id: UUID
    run_id: UUID
    request: dict[str, Any]
    completed_at: datetime
    runs: int


def plan_gap_resolution(
    gaps: Sequence[PendingGap],
    subject_signals: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary,
) -> tuple[GapResolution, ...]:
    """spec §4.9, "a tag newly mapped". A gap resolves to the mapped tag its suggested tag or
    normalised subject names (ScoringVocabulary.mapped_tag_for). Its regeneration input is its
    own active signals, then active signals naming the tag's slug or label as an unregistered
    subject -- passed explicitly, because signal tags are immutable and old signals would never
    match "overlapping tags" -- bounded like proposal context (spec note 2026-09-30)."""
    resolutions: list[GapResolution] = []
    for gap in sorted(gaps, key=lambda g: (g.created_at, str(g.proposal_id))):
        tag = vocabulary.mapped_tag_for(normalise_subject(gap.subject), gap.suggested_slug)
        if tag is None:
            continue
        keys = vocabulary.tag_keys(tag)
        matching = [
            s.signal_id
            for s in subject_signals
            if s.status == "active"
            and any(normalise_subject(u) in keys for u in s.unregistered_subjects)
        ]
        ids = tuple(dict.fromkeys([*gap.signal_ids, *matching]))[:PROPOSAL_CONTEXT_MAX_SIGNALS]
        resolutions.append(GapResolution(gap.proposal_id, tag, ids))
    return tuple(resolutions)


def plan_gap_retries(
    finished: Sequence[FinishedRegeneration], *, now: datetime
) -> tuple[FinishedRegeneration, ...]:
    """A refused or failed regeneration is queued again once it has been finished for
    GAP_REGENERATE_RETRY_HOURS, up to GAP_REGENERATE_MAX_RUNS runs per gap: five runs six hours
    apart always span a UTC-midnight budget reset, so a spent daily budget never loses the
    evidence behind a closed gap (spec note 2026-09-30)."""
    due = now - timedelta(hours=GAP_REGENERATE_RETRY_HOURS)
    return tuple(
        f
        for f in sorted(finished, key=lambda f: (f.completed_at, str(f.proposal_id)))
        if f.completed_at <= due and f.runs < GAP_REGENERATE_MAX_RUNS
    )


class Reconciler(Protocol):
    async def reconcile(self) -> ReconcileResult | None: ...
    async def resolve_gaps(self, now: datetime) -> GapPass: ...


class PostgresReconciler:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def reconcile(self) -> ReconcileResult | None:
        """None when there is no vocabulary, the pair is already reconciled, or the document
        cannot be read. In the last case nothing is recorded, so a corrected push is
        reconciled."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT release_no, map_version, scoring_version, quiz_version, document,"
                " reconciled_release_no, reconciled_map_version FROM intel_vocabulary"
                " WHERE id = 1 FOR UPDATE"
            )
            row = await cur.fetchone()
            if row is None:
                return None
            if (row["reconciled_release_no"], row["reconciled_map_version"]) == (
                row["release_no"],
                row["map_version"],
            ):
                return None
            vocabulary = parse_vocabulary(Vocabulary.model_validate(row))
            if vocabulary is None:
                return None
            await cur.execute(
                "SELECT proposal_id, target, against_release_no, created_at FROM intel_proposals"
                " WHERE kind = 'weight_change' AND status = 'pending' FOR UPDATE"
            )
            pending: list[PendingWeightChange] = []
            for p in await cur.fetchall():
                try:
                    target = WeightChangeTarget.model_validate(p["target"])
                except ValidationError:
                    log.error("intel.proposal_target_unreadable", proposal_id=str(p["proposal_id"]))
                    continue
                pending.append(
                    PendingWeightChange(
                        proposal_id=p["proposal_id"],
                        question_key=target.question_key,
                        option=target.option,
                        current=target.current,
                        against_release_no=(
                            p["against_release_no"] if p["against_release_no"] is not None else -1
                        ),
                        created_at=p["created_at"],
                    )
                )
            plan = plan_reconcile(
                pending, vocabulary, reconciled_release_no=row["reconciled_release_no"]
            )
            for proposal_id, _old, new in plan.retarget:
                await conn.execute(
                    "UPDATE intel_proposals SET target = jsonb_set(target, '{option}',"
                    " to_jsonb(%s::text)) WHERE proposal_id = %s AND status = 'pending'",
                    (new, proposal_id),
                )
            for proposal_id, reason in plan.supersede:
                await conn.execute(
                    "UPDATE intel_proposals SET status = 'superseded', supersede_reason = %s"
                    " WHERE proposal_id = %s AND status = 'pending'",
                    (reason, proposal_id),
                )
            await conn.execute(
                "UPDATE intel_vocabulary SET reconciled_release_no = %s,"
                " reconciled_map_version = %s WHERE id = 1",
                (row["release_no"], row["map_version"]),
            )
            if not plan.empty:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.vocabulary_reconciled",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "release_no": row["release_no"],
                                "map_version": row["map_version"],
                                "retargeted": [
                                    {"proposal_id": str(pid), "from": old, "to": new}
                                    for pid, old, new in plan.retarget
                                ],
                                "superseded": [
                                    {"proposal_id": str(pid), "reason": reason}
                                    for pid, reason in plan.supersede
                                ],
                            }
                        ),
                    },
                )
        result = ReconcileResult(
            release_no=row["release_no"],
            map_version=row["map_version"],
            retargeted=len(plan.retarget),
            superseded=len(plan.supersede),
        )
        log.info(
            "intel.vocabulary_reconciled",
            release_no=result.release_no,
            map_version=result.map_version,
            retargeted=result.retargeted,
            superseded=result.superseded,
        )
        return result

    async def resolve_gaps(self, now: datetime) -> GapPass:
        """spec §4.9 "a tag newly mapped", STATE-BASED (see the module docstring), in ONE
        transaction: resolve every pending gap the live quiz now maps, then queue again every
        regeneration the gate refused (plan_gap_retries). Nothing without a vocabulary."""
        if now.tzinfo is None:  # the stored completed_at is aware; never compare naive to it
            now = now.replace(tzinfo=UTC)
        resolved: list[tuple[GapResolution, UUID]] = []
        retried: list[tuple[UUID, UUID]] = []
        async with self._pool.connection() as conn, conn.transaction():
            vocabulary = await load_scoring_vocabulary(conn)
            if vocabulary is None:
                return GapPass(0, 0)
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(_LOCK_PENDING_GAPS_SQL)
            locked = [r["proposal_id"] for r in await cur.fetchall()]
            if locked:
                await cur.execute(_PENDING_GAPS_SQL, (locked,))
                gaps: list[PendingGap] = []
                for row in await cur.fetchall():
                    try:
                        target = CoverageGapTarget.model_validate(row["target"])
                    except ValidationError:
                        log.error(
                            "intel.proposal_target_unreadable", proposal_id=str(row["proposal_id"])
                        )
                        continue
                    gaps.append(
                        PendingGap(
                            proposal_id=row["proposal_id"],
                            subject=target.subject,
                            suggested_slug=(
                                target.suggested_tag.slug
                                if target.suggested_tag is not None
                                else None
                            ),
                            signal_ids=tuple(row["signal_ids"]),
                            created_at=row["created_at"],
                        )
                    )
                subject_signals = await active_subject_signals(
                    conn,
                    since=now - timedelta(days=COVERAGE_GAP_WINDOW_DAYS),
                    limit=COVERAGE_GAP_POOL_MAX,
                )
                for resolution in plan_gap_resolution(gaps, subject_signals, vocabulary):
                    run_id = await _queue_regeneration(
                        conn,
                        {
                            "coverage_gap_id": str(resolution.proposal_id),
                            "tag": resolution.tag,
                            "signal_ids": [str(i) for i in resolution.signal_ids],
                        },
                    )
                    await conn.execute(_RESOLVE_GAP_SQL, (str(run_id), resolution.proposal_id))
                    resolved.append((resolution, run_id))
            await cur.execute(_FINISHED_REGENERATIONS_SQL)
            finished = [
                FinishedRegeneration(
                    proposal_id=r["proposal_id"],
                    run_id=r["run_id"],
                    request=r["request"],
                    completed_at=r["completed_at"],
                    runs=r["runs"],
                )
                for r in await cur.fetchall()
            ]
            for retry in plan_gap_retries(finished, now=now):
                run_id = await _queue_regeneration(conn, retry.request)
                await conn.execute(
                    _FOLLOW_RETRY_SQL, (str(run_id), retry.proposal_id, str(retry.run_id))
                )
                retried.append((retry.proposal_id, run_id))
            if resolved or retried:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.coverage_gaps_resolved",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "resolved": [
                                    {
                                        "proposal_id": str(r.proposal_id),
                                        "tag": r.tag,
                                        "run_id": str(run_id),
                                    }
                                    for r, run_id in resolved
                                ],
                                "retried": [
                                    {"proposal_id": str(pid), "run_id": str(run_id)}
                                    for pid, run_id in retried
                                ],
                            }
                        ),
                    },
                )
        if resolved or retried:
            log.info("intel.coverage_gaps_resolved", resolved=len(resolved), retried=len(retried))
        return GapPass(resolved=len(resolved), retried=len(retried))
