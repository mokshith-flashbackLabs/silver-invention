"""The quiz-change reconcile (spec §4.9). Intel holds no copy of the quiz that can go stale
except the vocabulary, and it reacts to every new vocabulary on its own: within one poll of a
push, the pending weight changes match the live quiz, and nothing an operator approved has been
silently rewritten.

Deterministic and idempotent; no model runs here. ``plan_reconcile`` is pure, and
``PostgresReconciler`` applies its plan and records the reconciled pair in ONE transaction.

Step 2 handles weight_change rows only. Step 3 adds coverage-gap resolution (with
gap_regenerate), and that part must be state-based, because pairs reconciled before it ships
are already recorded here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from imageshield.intel.models import Vocabulary
from imageshield.intel.proposal_models import ReconcileResult, SupersedeReason, WeightChangeTarget
from imageshield.intel.vocabulary import ScoringVocabulary, parse_vocabulary

log = structlog.get_logger("imageshield.intel")

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""


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


class Reconciler(Protocol):
    async def reconcile(self) -> ReconcileResult | None: ...


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
