"""intel_proposals writes and reads (spec §3.6, §4.3, §4.7) against a real Postgres."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.proposal_models import NewProposal
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import (
    NOW,
    QUIZ_VOCABULARY,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
)

CHANGE = {"question_key": "platforms", "option": "Instagram", "current": 3}


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _run(pool: AsyncConnectionPool) -> UUID:
    return await PostgresIntelStore(pool).queue_adhoc("https://p.example/x", operator="a")


def _change(signal_id: UUID, **target: Any) -> NewProposal:
    return NewProposal("weight_change", {**CHANGE, **target}, {"delta": 1}, "because", (signal_id,))


async def _write(store: PostgresProposalStore, run_id: UUID, *proposals: NewProposal) -> Any:
    return await store.write_generated(
        run_id,
        list(proposals),
        against_scoring_version="s2",
        against_release_no=2,
        model_id="claude-opus-5-5",
        prompt_version="propose-v1",
    )


async def test_generation_writes_pending_rows_once_per_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    run_id = await _run(intel_pool)
    sid = await seed_signal(intel_pool, run_id=run_id, tags=("instagram",))
    store = PostgresProposalStore(intel_pool)
    assert not await store.proposals_written(run_id)
    result = await _write(store, run_id, _change(sid))
    assert result is not None and len(result.written) == 1 and result.superseded == ()
    assert await store.proposals_written(run_id)
    assert await _write(store, run_id, _change(sid)) is None  # a reclaimed run: never twice
    (row,) = await store.list_proposals(statuses=None, kinds=None, cursor=None, limit=10)
    assert row["status"] == "pending" and row["signal_ids"] == [sid]
    assert row["against_scoring_version"] == "s2" and row["against_release_no"] == 2
    assert row["run_id"] == run_id and row["decided"] is None


async def test_a_newer_pending_change_for_the_same_cell_supersedes_the_older(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresProposalStore(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    first = await _write(store, await _run(intel_pool), _change(sid))
    other = await _write(store, await _run(intel_pool), _change(sid, option="LinkedIn", current=2))
    second = await _write(store, await _run(intel_pool), _change(sid))
    assert second.superseded == first.written  # LinkedIn is another cell: untouched
    status, reason = await _scalar(
        intel_pool,
        "SELECT ARRAY[status, supersede_reason] FROM intel_proposals WHERE proposal_id = %s",
        first.written[0],
    )
    assert (status, reason) == ("superseded", "newer_proposal")
    assert (
        await _scalar(
            intel_pool,
            "SELECT status FROM intel_proposals WHERE proposal_id = %s",
            other.written[0],
        )
        == "pending"
    )


async def test_a_newer_gap_with_the_same_normalised_subject_supersedes_the_older(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresProposalStore(intel_pool)
    sid = await seed_signal(intel_pool, subjects=("Bumble",))
    gap = NewProposal("coverage_gap", {"subject": "Bumble"}, {}, "r", (sid,))
    later = NewProposal("coverage_gap", {"subject": "bumble!"}, {}, "r", (sid,))
    first = await _write(store, await _run(intel_pool), gap)
    second = await _write(store, await _run(intel_pool), later)
    assert second.superseded == first.written
    assert await _scalar(
        intel_pool,
        "SELECT against_release_no IS NULL FROM intel_proposals WHERE proposal_id = %s",
        second.written[0],
    )  # against_* is for weight kinds only


async def test_the_list_omits_weight_suggestion_unless_asked(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool)
    change = await seed_proposal(intel_pool, signal_ids=[sid])
    suggestion = await seed_proposal(
        intel_pool,
        signal_ids=[sid],
        kind="weight_suggestion",
        status="delivered",
        target={"question_key": "platforms", "options": []},
    )
    store = PostgresProposalStore(intel_pool)
    default = await store.list_proposals(statuses=None, kinds=None, cursor=None, limit=10)
    asked = await store.list_proposals(
        statuses=None, kinds=["weight_suggestion"], cursor=None, limit=10
    )
    assert [r["proposal_id"] for r in default] == [change]
    assert [r["proposal_id"] for r in asked] == [suggestion]
    assert asked[0]["why_not"] == "not_decidable" and asked[0]["approvable"] is False


async def test_the_list_filters_by_status_and_pages_by_keyset(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool)
    ids = [
        await seed_proposal(intel_pool, signal_ids=[sid], created_at=NOW - timedelta(minutes=m))
        for m in (3, 2, 1)
    ]
    await seed_proposal(intel_pool, signal_ids=[sid], status="rejected")
    store = PostgresProposalStore(intel_pool)
    page = await store.list_proposals(statuses=["pending"], kinds=None, cursor=None, limit=2)
    assert [r["proposal_id"] for r in page] == [ids[2], ids[1]]
    rest = await store.list_proposals(
        statuses=["pending"],
        kinds=None,
        cursor=(page[-1]["created_at"], page[-1]["proposal_id"]),
        limit=2,
    )
    assert [r["proposal_id"] for r in rest] == [ids[0]]


async def test_the_detail_carries_signals_excerpts_documents_and_read_flags(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sids = [
        await seed_signal(intel_pool, trust="web", publisher=p, tags=("instagram",))
        for p in ("a.example", "b.example")
    ]
    pid = await seed_proposal(intel_pool, signal_ids=sids)
    detail = await PostgresProposalStore(intel_pool).get_proposal(pid)
    assert detail is not None
    assert detail["approvable"] is True and detail["why_not"] is None
    assert detail["stale"] is False and detail["unmapped_tags"] == []
    assert {s["document"]["publisher_domain"] for s in detail["signals"]} == {
        "a.example",
        "b.example",
    }
    assert all(s["excerpts"] and s["excerpts"][0]["quote_text"] for s in detail["signals"])
    assert (
        await PostgresProposalStore(intel_pool).get_proposal(
            UUID("00000000-0000-0000-0000-000000000000")
        )
        is None
    )


async def test_a_pending_change_whose_deduction_just_moved_reads_stale(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 5, the read: a push landed and the reconcile has not run yet."""
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid])
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"]["Instagram"] = 5
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=doc)
    detail = await PostgresProposalStore(intel_pool).get_proposal(pid)
    assert detail is not None
    assert detail["stale"] is True and detail["why_stale"] == "deduction_moved"
    assert detail["approvable"] is True  # no 409 applies; the decision answers 422


async def test_retracting_every_signal_reads_evidence_retracted(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid])
    await PostgresEvidenceStore(intel_pool).retract_signal(sid, operator="a", reason="wrong page")
    detail = await PostgresProposalStore(intel_pool).get_proposal(pid)
    assert detail is not None
    assert detail["evidence_retracted"] is True and detail["why_not"] == "evidence_retracted"
    assert detail["approvable"] is False


async def test_context_reads(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    run_id = await _run(intel_pool)
    mine = await seed_signal(intel_pool, run_id=run_id, tags=("instagram",))
    by_tag = await seed_signal(intel_pool, tags=("instagram",), category="incident")
    by_category = await seed_signal(intel_pool, category="policy")
    unrelated = await seed_signal(intel_pool, category="law")
    too_old = await seed_signal(
        intel_pool, tags=("instagram",), created_at=NOW - timedelta(days=91)
    )
    gap_subject = await seed_signal(intel_pool, subjects=("Bumble",), category="law")
    unmapped = await seed_signal(intel_pool, tags=("linkedin",), category="law")
    store = PostgresProposalStore(intel_pool)
    assert [s.signal_id for s in await store.run_signals(run_id)] == [mine]
    related = await store.related_signals(
        exclude=[mine],
        tags=["instagram"],
        categories=["policy"],
        since=NOW - timedelta(days=90),
        limit=60,
    )
    assert {s.signal_id for s in related} == {by_tag, by_category}
    assert unrelated not in {s.signal_id for s in related}
    assert too_old not in {s.signal_id for s in related}
    pool_ids = {
        s.signal_id
        for s in await store.gap_candidates(
            since=NOW - timedelta(days=90), unmapped_tags=["linkedin"], limit=2000
        )
    }
    assert pool_ids == {gap_subject, unmapped}
