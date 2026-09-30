"""Decisions on intel proposals (spec §4.7). This is the ONE module that can move a proposal
to 'approved' (INVARIANTS #48; tests/test_boundaries.py holds it to that).

A decision is one transaction:
- the proposal row is taken FOR UPDATE;
- the vocabulary and the linked signals are read;
- the same predicate the reads use (intel/approvable.py) is re-checked, then §4.5 against the
  CURRENT vocabulary on the operator's final delta;
- the transition is written guarded by its from-status;
- the audit row goes in the same transaction.

Two reviewers therefore produce one outcome and one clean 409, and two approvals of
different proposals for one cell meet the partial unique index
(intel_proposals_one_approved_per_cell): one success and one clean
proposal_cell_awaiting_publish.

The acknowledgement (``mark_applied``) is the second system write (spec §4.7). It moves only
approved weight changes, keeps the approval's decided_by, decided_at and decision_reason, and
is audited only for rows that actually move.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from imageshield.intel.approvable import APPROVABLE_KINDS, REJECTABLE_KINDS, why_not
from imageshield.intel.cells import cell_problem, published_unacknowledged
from imageshield.intel.proposal_models import (
    AppliedResult,
    ContextSignal,
    Decided,
    DecisionRefusal,
    DecisionRefused,
    ProposalRecord,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.proposal_store import (
    PROPOSAL_COLUMNS,
    fetch_linked_signals,
    load_scoring_vocabulary,
    record_of,
)
from imageshield.intel.vocabulary import ScoringVocabulary

log = structlog.get_logger("imageshield.intel")

_CELL_INDEX = "intel_proposals_one_approved_per_cell"

_MESSAGES: dict[DecisionRefusal, str] = {
    "proposal_not_found": "No proposal with this id.",
    "proposal_not_pending": "This proposal is no longer open to this decision.",
    "proposal_not_decidable": "This kind of proposal cannot take this decision.",
    "proposal_evidence_retracted": "Every signal behind this proposal has been retracted.",
    "proposal_uncorroborated": "Web-only evidence needs a second, independent publisher.",
    "proposal_tags_unmapped": "No option of the live quiz maps to any of this event's tags.",
    "proposal_cell_awaiting_publish": "Another approved change for this option awaits publish.",
    "values_out_of_bounds": "These values fall outside the bounds for this option.",
}

_WHY_NOT_REFUSAL: dict[str, DecisionRefusal] = {
    "not_decidable": "proposal_not_decidable",
    "evidence_retracted": "proposal_evidence_retracted",
    "uncorroborated": "proposal_uncorroborated",
    "tags_unmapped": "proposal_tags_unmapped",
}

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_APPROVE_SQL = """
    UPDATE intel_proposals SET status = 'approved', decided = %(decided)s,
           decided_by = %(operator)s, decided_at = now(), decision_reason = %(reason)s
     WHERE proposal_id = %(proposal_id)s AND status = 'pending'
    RETURNING status, applied_ref, decided
"""

_REJECT_SQL = """
    UPDATE intel_proposals SET status = 'rejected', decided_by = %(operator)s,
           decided_at = now(), decision_reason = %(reason)s
     WHERE proposal_id = %(proposal_id)s AND status = %(from_status)s
    RETURNING status, applied_ref, decided
"""

_APPLIED_SQL = """
    UPDATE intel_proposals SET status = 'applied', applied_ref = %(scoring_version)s
     WHERE proposal_id = ANY(%(ids)s::uuid[]) AND kind = 'weight_change' AND status = 'approved'
    RETURNING proposal_id
"""


def _refuse(code: DecisionRefusal) -> DecisionRefused:
    return DecisionRefused(code, _MESSAGES[code])


def _approval_decided(
    proposal: ProposalRecord,
    active: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
) -> dict[str, Any]:
    """The exact values an approval stores, or a refusal. Step 2 approves weight changes
    only; steps 3 and 4 add the event branches here."""
    if proposal.kind not in APPROVABLE_KINDS:
        raise _refuse("proposal_not_decidable")
    if proposal.status != "pending":
        raise _refuse("proposal_not_pending")
    reason = why_not(proposal, active, vocabulary)
    if reason is not None:
        raise _refuse(_WHY_NOT_REFUSAL[reason])
    try:
        target = WeightChangeTarget.model_validate(proposal.target)
        delta = WeightDelta.model_validate(values if values is not None else proposal.suggested)
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    if vocabulary is None or cell_problem(
        vocabulary,
        question_key=target.question_key,
        option=target.option,
        current=target.current,
        delta=delta.delta,
    ):
        raise _refuse("values_out_of_bounds")
    return delta.model_dump()


def _rejection_from(proposal: ProposalRecord, vocabulary: ScoringVocabulary | None) -> str:
    """The status a rejection moves FROM: pending, or approved for a withdrawal of an
    unapplied weight change. A change the live quiz shows as published but unacknowledged is
    never withdrawn (spec §4.9): a weight in force is never recorded as rejected."""
    if proposal.kind not in REJECTABLE_KINDS:
        raise _refuse("proposal_not_decidable")
    if proposal.status == "pending":
        return "pending"
    if proposal.status == "approved" and proposal.kind == "weight_change":
        if vocabulary is not None and proposal.decided is not None:
            target = WeightChangeTarget.model_validate(proposal.target)
            delta = WeightDelta.model_validate(proposal.decided)
            if published_unacknowledged(
                vocabulary,
                question_key=target.question_key,
                option=target.option,
                current=target.current,
                delta=delta.delta,
            ):
                raise _refuse("proposal_not_pending")
        return "approved"
    raise _refuse("proposal_not_pending")


class DecisionStore(Protocol):
    async def decide(
        self,
        proposal_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
        values: dict[str, Any] | None,
        reason: str,
        operator: str,
    ) -> Decided: ...
    async def mark_applied(
        self, *, scoring_version: str, proposal_ids: Sequence[UUID]
    ) -> AppliedResult: ...


class PostgresDecisionStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def decide(
        self,
        proposal_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
        values: dict[str, Any] | None,
        reason: str,
        operator: str,
    ) -> Decided:
        try:
            async with self._pool.connection() as conn, conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    f"SELECT {PROPOSAL_COLUMNS} FROM intel_proposals"
                    " WHERE proposal_id = %s FOR UPDATE",
                    (proposal_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise _refuse("proposal_not_found")
                proposal = record_of(row)
                vocabulary = await load_scoring_vocabulary(conn)
                if decision == "approved":
                    linked = (await fetch_linked_signals(conn, [proposal_id]))[proposal_id]
                    active = [s for s in linked if s.status == "active"]
                    decided = _approval_decided(proposal, active, vocabulary, values)
                    from_status = "pending"
                    await cur.execute(
                        _APPROVE_SQL,
                        {
                            "proposal_id": proposal_id,
                            "decided": Jsonb(decided),
                            "operator": operator,
                            "reason": reason,
                        },
                    )
                else:
                    from_status = _rejection_from(proposal, vocabulary)
                    await cur.execute(
                        _REJECT_SQL,
                        {
                            "proposal_id": proposal_id,
                            "from_status": from_status,
                            "operator": operator,
                            "reason": reason,
                        },
                    )
                updated = await cur.fetchone()
                if updated is None:  # belt and braces: the row is locked
                    raise _refuse("proposal_not_pending")
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "operator",
                        "action": "intel.proposal_decided",
                        "resource_id": proposal_id,
                        "metadata": Jsonb(
                            {
                                "operator": operator,
                                "kind": proposal.kind,
                                "decision": decision,
                                "from_status": from_status,
                                "decided": updated["decided"],
                                "reason": reason,
                            }
                        ),
                    },
                )
        except UniqueViolation as exc:
            if exc.diag.constraint_name == _CELL_INDEX:
                raise _refuse("proposal_cell_awaiting_publish") from exc
            raise
        return Decided(
            proposal_id=proposal_id,
            kind=proposal.kind,
            status=updated["status"],
            applied_ref=updated["applied_ref"],
            decided=updated["decided"],
        )

    async def mark_applied(
        self, *, scoring_version: str, proposal_ids: Sequence[UUID]
    ) -> AppliedResult:
        ids = list(dict.fromkeys(proposal_ids))
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_APPLIED_SQL, {"scoring_version": scoring_version, "ids": ids})
            moved = [r[0] for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT proposal_id FROM intel_proposals WHERE proposal_id = ANY(%s::uuid[])"
                " AND kind = 'weight_change' AND status = 'applied'",
                (ids,),
            )
            applied_now = {r[0] for r in await cur.fetchall()}
            if moved:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.proposals_applied",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "scoring_version": scoring_version,
                                "proposal_ids": [str(i) for i in moved],
                            }
                        ),
                    },
                )
        moved_set = set(moved)
        return AppliedResult(
            applied=tuple(moved),
            already_applied=tuple(i for i in ids if i in applied_now and i not in moved_set),
            not_applied=tuple(i for i in ids if i not in applied_now),
        )
