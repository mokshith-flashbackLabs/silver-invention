"""Operator decisions and the applied acknowledgement (spec §3.6, §4.7, §4.9, §10)."""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.proposal_models import Decided, DecisionRefused
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.publisher import publisher_domain
from tests.intel_fakes import QUIZ_VOCABULARY, seed_proposal, seed_quiz_vocabulary, seed_signal


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _approvable(pool: AsyncConnectionPool, **kw: Any) -> UUID:
    sid = await seed_signal(pool, tags=("instagram",))  # listed: corroborated alone
    return await seed_proposal(pool, signal_ids=[sid], **kw)


async def _decide(
    pool: AsyncConnectionPool,
    pid: UUID,
    decision: str = "approved",
    values: dict[str, Any] | None = None,
    operator: str = "ann",
) -> Decided:
    return await PostgresDecisionStore(pool).decide(
        pid,
        decision=decision,
        values=values,
        reason="checked the sources",  # type: ignore[arg-type]
        operator=operator,
    )


async def _refused(
    pool: AsyncConnectionPool,
    pid: UUID,
    decision: str = "approved",
    values: dict[str, Any] | None = None,
) -> str:
    with pytest.raises(DecisionRefused) as caught:
        await _decide(pool, pid, decision, values)
    return caught.value.code


async def test_approve_stores_decided_from_suggested_and_names_the_operator(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _approvable(intel_pool)
    decided = await _decide(intel_pool, pid)
    assert (decided.status, decided.decided, decided.kind) == (
        "approved",
        {"delta": 1},
        "weight_change",
    )
    row = await _scalar(
        intel_pool,
        "SELECT ARRAY[decided_by, decision_reason] FROM intel_proposals WHERE proposal_id = %s",
        pid,
    )
    assert row == ["ann", "checked the sources"]
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.proposal_decided'"
        )
        == 1
    )


async def test_an_operator_edit_wins_and_an_out_of_bounds_edit_is_refused(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    for bad in (
        {"delta": 5},
        {"delta": 1.5},
        {"delta": True},
        {"delta": 1, "current": 2},
        {"current": 4},
    ):
        pid = await _approvable(intel_pool)
        assert await _refused(intel_pool, pid, values=bad) == "values_out_of_bounds"
        assert (
            await _scalar(
                intel_pool, "SELECT status FROM intel_proposals WHERE proposal_id = %s", pid
            )
            == "pending"
        )
    pid = await _approvable(
        intel_pool, target={"question_key": "platforms", "option": "LinkedIn", "current": 2}
    )
    assert (await _decide(intel_pool, pid, values={"delta": -1})).decided == {"delta": -1}


@pytest.mark.parametrize(
    ("urls", "trust", "approvable"),
    [
        (["https://a.example/x"], "web", False),
        (["https://a.news.example.co.uk/x", "https://b.news.example.co.uk/y"], "web", False),
        (["https://a.example/x", "https://b.example/y"], "web", True),
        (["https://a.example/x"], "listed", True),
    ],
)
async def test_approvable_on_the_read_equals_the_decision_not_409ing(
    intel_pool: AsyncConnectionPool, urls: list[str], trust: str, approvable: bool
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sids = [
        await seed_signal(
            intel_pool, trust=trust, publisher=publisher_domain(u), tags=("instagram",)
        )
        for u in urls
    ]
    pid = await seed_proposal(intel_pool, signal_ids=sids)
    read = await PostgresProposalStore(intel_pool).get_proposal(pid)
    assert read is not None and read["approvable"] is approvable
    if approvable:
        await _decide(intel_pool, pid)
    else:
        assert await _refused(intel_pool, pid) == "proposal_uncorroborated"


async def test_two_simultaneous_approvals_of_one_proposal(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _approvable(intel_pool)
    results = await asyncio.gather(
        _decide(intel_pool, pid, operator="ann"),
        _decide(intel_pool, pid, operator="bob"),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["Decided", "DecisionRefused"]
    (refused,) = [r for r in results if isinstance(r, DecisionRefused)]
    assert refused.code == "proposal_not_pending"


async def test_two_proposals_for_one_cell_one_waits_for_publish(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    first, second = await _approvable(intel_pool), await _approvable(intel_pool)
    results = await asyncio.gather(
        _decide(intel_pool, first), _decide(intel_pool, second), return_exceptions=True
    )
    (refused,) = [r for r in results if isinstance(r, DecisionRefused)]
    assert refused.code == "proposal_cell_awaiting_publish"
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM intel_proposals WHERE status = 'approved'")
        == 1
    )


@pytest.mark.parametrize(
    ("same_proposal", "code"),
    [(True, "proposal_not_pending"), (False, "proposal_cell_awaiting_publish")],
)
async def test_a_decision_blocked_behind_an_uncommitted_approval_gets_the_clean_409(
    intel_pool: AsyncConnectionPool, same_proposal: bool, code: str
) -> None:
    """The race FORCED rather than hoped for: ``gather`` above may run the two decisions one
    after the other. Here the rival approval holds its row lock (or its cell's index entry)
    uncommitted when the decision starts, so the decision must wait on it -- and then refuse
    from inside its own transaction, never write a second approval."""
    await seed_quiz_vocabulary(intel_pool)
    rival = await _approvable(intel_pool)
    contested = rival if same_proposal else await _approvable(intel_pool)
    async with intel_pool.connection() as holder:
        async with holder.transaction():
            await holder.execute(
                "UPDATE intel_proposals SET status = 'approved', decided = '{\"delta\": 1}',"
                " decided_by = 'rival', decided_at = now(), decision_reason = 'first'"
                " WHERE proposal_id = %s",
                (rival,),
            )
            blocked = asyncio.create_task(_decide(intel_pool, contested, operator="bob"))
            await asyncio.sleep(0.5)
            assert not blocked.done()  # it is waiting on the rival, not racing past it
        with pytest.raises(DecisionRefused) as caught:
            await blocked
    assert caught.value.code == code
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM intel_proposals WHERE status = 'approved'")
        == 1
    )
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.proposal_decided'"
        )
        == 0
    )


async def test_reject_withdraw_and_a_published_change_cannot_be_withdrawn(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pending = await _approvable(intel_pool)
    assert (await _decide(intel_pool, pending, "rejected")).status == "rejected"
    approved = await _approvable(intel_pool, status="approved", decided={"delta": 1})
    withdrawn = await _decide(intel_pool, approved, "rejected", operator="cat")
    assert withdrawn.status == "rejected" and withdrawn.decided == {"delta": 1}
    assert (
        await _scalar(
            intel_pool, "SELECT decided_by FROM intel_proposals WHERE proposal_id = %s", approved
        )
        == "cat"
    )
    published = await _approvable(intel_pool, status="approved", decided={"delta": 1})
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"]["Instagram"] = 4  # the backend published 3 + 1
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=doc)
    assert await _refused(intel_pool, published, "rejected") == "proposal_not_pending"


async def test_a_gap_is_dismissed_never_approved(intel_pool: AsyncConnectionPool) -> None:
    sid = await seed_signal(intel_pool, subjects=("Bumble",))
    gap = await seed_proposal(
        intel_pool, signal_ids=[sid], kind="coverage_gap", target={"subject": "Bumble"}
    )
    assert await _refused(intel_pool, gap) == "proposal_not_decidable"
    assert (await _decide(intel_pool, gap, "rejected")).status == "rejected"


@pytest.mark.parametrize(
    ("kind", "status", "target"),
    [
        ("threat_event", "pending", {"tags": ["instagram"]}),
        ("protection_event", "pending", {"tags": ["instagram"], "is_global": False}),
        ("weight_suggestion", "delivered", {"question_key": "platforms", "options": []}),
    ],
)
async def test_kinds_whose_step_has_not_shipped_are_not_decidable(
    intel_pool: AsyncConnectionPool, kind: str, status: str, target: dict[str, Any]
) -> None:
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid], kind=kind, status=status, target=target)
    assert await _refused(intel_pool, pid) == "proposal_not_decidable"
    assert await _refused(intel_pool, pid, "rejected") == "proposal_not_decidable"


async def test_retracted_evidence_refuses_approval(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid])
    await PostgresEvidenceStore(intel_pool).retract_signal(sid, operator="a", reason="wrong")
    assert await _refused(intel_pool, pid) == "proposal_evidence_retracted"


async def test_a_stale_pending_change_is_refused_values_out_of_bounds(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 5, the decision: the push landed, the reconcile has not run."""
    await seed_quiz_vocabulary(intel_pool)
    pid = await _approvable(intel_pool)
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"]["Instagram"] = 5
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=doc)
    assert await _refused(intel_pool, pid) == "values_out_of_bounds"
    assert (
        await _scalar(intel_pool, "SELECT status FROM intel_proposals WHERE proposal_id = %s", pid)
        == "pending"
    )


async def test_an_unknown_proposal_is_not_found_and_a_refusal_writes_no_audit_row(
    intel_pool: AsyncConnectionPool,
) -> None:
    assert await _refused(intel_pool, uuid4()) == "proposal_not_found"
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.proposal_decided'"
        )
        == 0
    )


async def test_applied_moves_approved_changes_once_and_keeps_the_approval(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _approvable(intel_pool, status="approved", decided={"delta": 1})
    store = PostgresDecisionStore(intel_pool)
    first = await store.mark_applied(scoring_version="s3", proposal_ids=[pid, pid])
    assert first.applied == (pid,) and first.already_applied == () and first.not_applied == ()
    row = await _scalar(
        intel_pool,
        "SELECT ARRAY[status, applied_ref, decided_by] FROM intel_proposals WHERE proposal_id = %s",
        pid,
    )
    assert row == ["applied", "s3", "seed-op"]
    again = await store.mark_applied(scoring_version="s3", proposal_ids=[pid])
    assert again.applied == () and again.already_applied == (pid,)
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.proposals_applied'"
        )
        == 1
    )


async def test_an_ack_naming_unknown_pending_or_withdrawn_ids_moves_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 3."""
    await seed_quiz_vocabulary(intel_pool)
    pending = await _approvable(intel_pool)
    withdrawn = await _approvable(intel_pool, status="rejected")
    unknown = uuid4()
    result = await PostgresDecisionStore(intel_pool).mark_applied(
        scoring_version="s3", proposal_ids=[pending, withdrawn, unknown]
    )
    assert result.applied == () and result.already_applied == ()
    assert set(result.not_applied) == {pending, withdrawn, unknown}
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.proposals_applied'"
        )
        == 0
    )
    assert (
        await _scalar(
            intel_pool, "SELECT status FROM intel_proposals WHERE proposal_id = %s", withdrawn
        )
        == "rejected"
    )


async def test_the_shape_check_refuses_an_approved_change_without_decided(
    intel_pool: AsyncConnectionPool,
) -> None:
    # pytest.raises OUTSIDE the pool block: the failed statement must roll the block back,
    # never be committed over.
    with pytest.raises(psycopg.errors.CheckViolation):
        async with intel_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO intel_proposals (kind, status, target, suggested, rationale,"
                " model_id, prompt_version, decided_by, decided_at, decision_reason)"
                " VALUES ('weight_change', 'approved', '{}', '{}', 'r', 'm', 'p', 'ann', now(),"
                " 'ok')"
            )
