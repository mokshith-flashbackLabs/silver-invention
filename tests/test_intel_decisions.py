"""Operator decisions and the applied acknowledgement (spec §3.6, §4.7, §4.9, §10)."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.proposal_models import Decided, DecisionRefused
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.publisher import publisher_domain
from tests.intel_fakes import (
    QUIZ_VOCABULARY,
    THREAT_SUGGESTED,
    mapped_document,
    quiz_document,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_proposal,
)


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


# -- threat events (step 3) ---------------------------------------------------


async def _threat_approvable(pool: AsyncConnectionPool, **kw: Any) -> UUID:
    sid = await seed_signal(pool, tags=("instagram",))  # listed: corroborated alone
    return await seed_threat_proposal(pool, signal_ids=[sid], **kw)


async def test_approving_a_threat_creates_the_event_from_decided_in_one_transaction(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    decided = await _decide(intel_pool, pid)
    assert (decided.kind, decided.status) == ("threat_event", "applied")
    assert decided.decided == {**THREAT_SUGGESTED, "tags": ["instagram"]}
    assert decided.applied_ref is not None
    event_id = UUID(decided.applied_ref)
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind, title, severity, tags, domains, is_global, status, created_by,"
            " proposal_id, decay_days, expires_at - starts_at, body"
            " FROM threat_events WHERE event_id = %s",
            (event_id,),
        )
        row = await cur.fetchone()
    assert row == (
        "leak",
        THREAT_SUGGESTED["title"],
        3,
        ["instagram"],
        [],
        False,
        "active",
        "ann",
        pid,
        30,
        timedelta(days=30),
        "",
    )
    assert await _scalar(
        intel_pool,
        "SELECT ARRAY[direction, magnitude::text] FROM svc.v_active_scoped_events"
        " WHERE event_id = %s",
        event_id,
    ) == ["threat", "3"]
    metadata = await _scalar(
        intel_pool, "SELECT metadata FROM audit_log WHERE action = 'intel.proposal_decided'"
    )
    assert metadata["event_id"] == str(event_id) and metadata["operator"] == "ann"


async def test_a_partial_edit_changes_only_what_it_names_and_keeps_the_proposals_tags(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The backend may send values without tags (its §6.3): the proposal's own tags stand."""
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    decided = await _decide(intel_pool, pid, values={"severity": 5, "title": "Edited title"})
    assert decided.decided == {
        **THREAT_SUGGESTED,
        "severity": 5,
        "title": "Edited title",
        "tags": ["instagram"],
    }
    assert await _scalar(
        intel_pool,
        "SELECT ARRAY[title, severity::text] FROM threat_events WHERE proposal_id = %s",
        pid,
    ) == ["Edited title", "5"]


async def test_an_out_of_bounds_threat_edit_is_refused_and_writes_no_event(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    for bad in (
        {"severity": 6},
        {"severity": 2.5},
        {"severity": True},
        {"expires_in_days": 91},
        {"expires_in_days": 0},
        {"kind": "tsunami"},
        {"title": "   "},
        {"tags": []},
        {"tags": ["Instagram"]},
        {"tags": ["instagram", "instagram"]},
        {"body": "a threat carries no body"},
        {"delta": 1},
    ):
        pid = await _threat_approvable(intel_pool)
        assert await _refused(intel_pool, pid, values=bad) == "values_out_of_bounds", bad
        assert (
            await _scalar(
                intel_pool, "SELECT status FROM intel_proposals WHERE proposal_id = %s", pid
            )
            == "pending"
        )
    assert await _scalar(intel_pool, "SELECT count(*) FROM threat_events") == 0


async def test_adding_an_unregistered_or_retired_tag_is_refused_naming_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    with pytest.raises(DecisionRefused) as unknown:
        await _decide(intel_pool, pid, values={"tags": ["instagram", "tiktok"]})
    assert (unknown.value.code, unknown.value.slugs) == ("unknown_tag", ("tiktok",))
    with pytest.raises(DecisionRefused) as retired:
        await _decide(intel_pool, pid, values={"tags": ["instagram", "myspace"]})
    assert (retired.value.code, retired.value.slugs) == ("tag_retired", ("myspace",))
    assert await _scalar(intel_pool, "SELECT count(*) FROM threat_events") == 0


async def test_a_proposal_whose_own_tag_was_retired_since_is_still_approvable(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: retiring refuses NEW uses only. Retiring never unmaps an option, so the tag
    stays mapped, and it is not 'added' when it is the proposal's own."""
    doc = quiz_document()
    doc["tags"][0]["retired"] = True  # instagram, still mapped to the Instagram option
    await seed_quiz_vocabulary(intel_pool, document=doc)
    first = await _threat_approvable(intel_pool)
    decided = (await _decide(intel_pool, first)).decided
    assert decided is not None and decided["tags"] == ["instagram"]
    second = await _threat_approvable(intel_pool)
    assert (await _decide(intel_pool, second, values={"tags": ["instagram"]})).status == "applied"


async def test_an_all_unmapped_threat_waits_then_becomes_approvable_when_a_push_maps_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("linkedin",))
    pid = await seed_threat_proposal(intel_pool, signal_ids=[sid], tags=("linkedin",))
    store = PostgresProposalStore(intel_pool)
    read = await store.get_proposal(pid)
    assert read is not None and (read["approvable"], read["why_not"]) == (False, "tags_unmapped")
    assert await _refused(intel_pool, pid) == "proposal_tags_unmapped"
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    read = await store.get_proposal(pid)
    assert read is not None and read["approvable"] is True
    assert (await _decide(intel_pool, pid)).status == "applied"


async def test_an_edit_that_leaves_only_unmapped_tags_is_refused(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 3: an edit must not create an event that reaches nobody."""
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram", "linkedin"))
    pid = await seed_threat_proposal(intel_pool, signal_ids=[sid], tags=("instagram", "linkedin"))
    assert (
        await _refused(intel_pool, pid, values={"tags": ["linkedin"]}) == "proposal_tags_unmapped"
    )
    assert await _scalar(intel_pool, "SELECT count(*) FROM threat_events") == 0


async def test_two_simultaneous_threat_approvals_make_one_event(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    results = await asyncio.gather(
        _decide(intel_pool, pid, operator="ann"),
        _decide(intel_pool, pid, operator="bob"),
        return_exceptions=True,
    )
    (refused,) = [r for r in results if isinstance(r, DecisionRefused)]
    assert refused.code == "proposal_not_pending"
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM threat_events WHERE proposal_id = %s", pid)
        == 1
    )


async def test_rejecting_a_threat_writes_no_event(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    rejected = await _decide(intel_pool, pid, "rejected")
    assert (rejected.status, rejected.applied_ref, rejected.decided) == ("rejected", None, None)
    assert await _scalar(intel_pool, "SELECT count(*) FROM threat_events") == 0


@pytest.fixture
async def intel_rw_pool(intel_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose sessions run AS ``intel_rw``. Every other test here connects as the
    superuser, which hides a missing grant (the 0035 trap)."""
    sep = "&" if "?" in intel_db else "?"
    pool = make_async_pool(f"{intel_db}{sep}options=-c%20role%3Dintel_rw", min_size=1, max_size=2)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def test_a_threat_approval_needs_no_grant_intel_rw_lacks(
    intel_pool: AsyncConnectionPool, intel_rw_pool: AsyncConnectionPool
) -> None:
    """The whole decision transaction -- the row lock, the reads, the event insert, the
    proposal update and the audit row -- runs AS intel_rw, the role 0042 grants the insert to.
    An edited expiry reaches the row, and the inert decay_days follows it (spec §4.5)."""
    await seed_quiz_vocabulary(intel_pool)
    pid = await _threat_approvable(intel_pool)
    assert await _scalar(intel_rw_pool, "SELECT current_user") == "intel_rw"
    decided = await _decide(intel_rw_pool, pid, values={"expires_in_days": 7})
    assert decided.status == "applied" and decided.applied_ref is not None
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT proposal_id, decay_days, expires_at - starts_at FROM threat_events"
            " WHERE event_id = %s",
            (UUID(decided.applied_ref),),
        )
        assert await cur.fetchone() == (pid, 7, timedelta(days=7))
