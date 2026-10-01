"""intel_proposals writes and reads (spec §3.6, §4.3, §4.7) against a real Postgres."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.proposal_models import Attachment, NewProposal
from imageshield.intel.proposal_store import PostgresProposalStore, active_subject_signals
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import (
    NOW,
    PROTECTION_SUGGESTED,
    QUIZ_VOCABULARY,
    THREAT_SUGGESTED,
    protection_decided,
    seed_proposal,
    seed_protection_event,
    seed_protection_proposal,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
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


async def _write(
    store: PostgresProposalStore,
    run_id: UUID,
    *proposals: NewProposal,
    attachments: Sequence[Attachment] = (),
) -> Any:
    return await store.write_generated(
        run_id,
        list(proposals),
        against_scoring_version="s2",
        against_release_no=2,
        model_id="claude-opus-5-5",
        prompt_version="propose-v1",
        attachments=attachments,
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


def _threat(signal_id: UUID, *, tags: tuple[str, ...] = ("instagram",)) -> NewProposal:
    return NewProposal(
        "threat_event", {"tags": list(tags)}, dict(THREAT_SUGGESTED), "because", (signal_id,)
    )


async def test_a_threat_proposal_is_written_pending_and_supersedes_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    store = PostgresProposalStore(intel_pool)
    first = await _write(store, await _run(intel_pool), _threat(sid))
    second = await _write(store, await _run(intel_pool), _threat(sid))
    assert second.superseded == () and len(second.written) == 1
    rows = await store.list_proposals(
        statuses=["pending"], kinds=["threat_event"], cursor=None, limit=10
    )
    assert {r["proposal_id"] for r in rows} == {*first.written, *second.written}
    assert all(r["against_release_no"] is None for r in rows)  # against_* is weight kinds only
    assert rows[0]["target"] == {"tags": ["instagram"]}
    assert rows[0]["suggested"] == THREAT_SUGGESTED


async def test_an_attachment_adds_evidence_to_a_pending_event_proposal_and_nothing_else(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 5: a target dismissed since the model call, or of another kind, is dropped
    and counted, and the run's write still commits."""
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    dismissed = await seed_threat_proposal(intel_pool, signal_ids=[old], status="rejected")
    change = await seed_proposal(intel_pool, signal_ids=[old])  # a weight change, not an event
    run_id = await _run(intel_pool)
    new = await seed_signal(intel_pool, run_id=run_id, tags=("instagram",), publisher="b.example")
    store = PostgresProposalStore(intel_pool)
    result = await store.write_generated(
        run_id,
        [],
        against_scoring_version="s2",
        against_release_no=2,
        model_id="claude-opus-5-5",
        prompt_version="propose-v2",
        attachments=[
            Attachment(pending, (new,)),
            Attachment(pending, (new,)),  # a repeat link is a no-op
            Attachment(dismissed, (new,)),
            Attachment(change, (new,)),
        ],
    )
    assert result is not None
    assert result.attached == (pending,) and result.attach_dropped == 2
    detail = await store.get_proposal(pending)
    assert detail is not None and set(detail["signal_ids"]) == {old, new}
    assert detail["target"] == {"tags": ["instagram"]}
    assert detail["suggested"] == THREAT_SUGGESTED and detail["rationale"] == "seeded"
    assert await store.proposals_written(run_id)


async def test_pending_event_proposals_are_the_open_overlapping_ones_with_their_documents(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    s1 = await seed_signal(intel_pool, tags=("instagram",))
    s2 = await seed_signal(intel_pool, tags=("linkedin",))
    insta = await seed_threat_proposal(intel_pool, signal_ids=[s1])
    await seed_threat_proposal(intel_pool, signal_ids=[s2], tags=("linkedin",))
    await seed_threat_proposal(intel_pool, signal_ids=[s1], status="rejected")
    await seed_proposal(intel_pool, signal_ids=[s1])
    store = PostgresProposalStore(intel_pool)
    (found,) = await store.pending_event_proposals(tags=["instagram"], limit=40)
    assert found.proposal_id == insta and found.kind == "threat_event"
    assert found.tags == ("instagram",) and found.signal_ids == (s1,)
    assert found.title == THREAT_SUGGESTED["title"] and found.severity == 3
    page = await _scalar(
        intel_pool,
        "SELECT d.url_hash FROM intel_signals s JOIN intel_documents d USING (document_id)"
        " WHERE s.signal_id = %s",
        s1,
    )
    assert found.document_keys == frozenset({page})
    assert await store.pending_event_proposals(tags=[], limit=40) == []


async def test_pending_event_proposals_read_only_active_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Final review M1: a retracted signal is excluded everywhere (spec §3.5). It leaves a
    proposal's ids, pages and categories, and a proposal left with none is no candidate."""
    await seed_quiz_vocabulary(intel_pool)
    kept = await seed_signal(intel_pool, tags=("instagram",), category="incident")
    gone = await seed_signal(intel_pool, tags=("instagram",), category="policy")
    lone = await seed_signal(intel_pool, tags=("instagram",))
    both = await seed_threat_proposal(intel_pool, signal_ids=[kept, gone])
    await seed_threat_proposal(intel_pool, signal_ids=[lone])
    evidence = PostgresEvidenceStore(intel_pool)
    for sid in (gone, lone):
        assert await evidence.retract_signal(sid, operator="ann", reason="wrong") == "retracted"
    (found,) = await PostgresProposalStore(intel_pool).pending_event_proposals(
        tags=["instagram"], limit=40
    )
    page = await _scalar(
        intel_pool,
        "SELECT d.url_hash FROM intel_signals s JOIN intel_documents d USING (document_id)"
        " WHERE s.signal_id = %s",
        kept,
    )
    assert found.proposal_id == both and found.signal_ids == (kept,)
    assert found.document_keys == frozenset({page})
    assert found.categories == frozenset({"incident"})


async def test_active_threat_events_are_live_tagged_and_carry_the_approvals_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool, tags=("instagram",))
    applied = await seed_threat_proposal(
        intel_pool,
        signal_ids=[sid],
        status="applied",
        decided={**THREAT_SUGGESTED, "tags": ["instagram"]},
    )
    live = await seed_threat_event(intel_pool, proposal_id=applied, title="live")
    by_hand = await seed_threat_event(intel_pool, title="by hand", domains=("evil.example",))
    await seed_threat_event(intel_pool, title="retracted", status="retracted")
    await seed_threat_event(intel_pool, title="expired", starts_in_days=-10, ends_in_days=-1)
    await seed_threat_event(intel_pool, title="untagged", tags=(), domains=("evil.example",))
    await seed_threat_event(intel_pool, title="other tag", tags=("linkedin",))
    events = await PostgresProposalStore(intel_pool).active_threat_events(
        tags=["instagram"], limit=40
    )
    assert {e.title for e in events} == {"live", "by hand"}
    (approved,) = [e for e in events if e.event_id == live]
    assert approved.signal_ids == (sid,) and approved.proposal_id == applied
    (hand,) = [e for e in events if e.event_id == by_hand]
    assert hand.signal_ids == () and hand.proposal_id is None


async def test_signals_by_id_returns_the_active_ones_with_their_documents(
    intel_pool: AsyncConnectionPool,
) -> None:
    keep = await seed_signal(intel_pool, subjects=("LinkedIn",))
    gone = await seed_signal(intel_pool, subjects=("LinkedIn",))
    await PostgresEvidenceStore(intel_pool).retract_signal(gone, operator="a", reason="wrong")
    found = await PostgresProposalStore(intel_pool).signals_by_id([keep, gone, uuid4()])
    assert [s.signal_id for s in found] == [keep] and found[0].document_key is not None


async def test_the_detail_read_names_related_live_events_for_event_kinds_only(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    proposal = await seed_threat_proposal(intel_pool, signal_ids=[sid])
    event_id = await seed_threat_event(intel_pool, title="Live breach")
    change = await seed_proposal(intel_pool, signal_ids=[sid])
    store = PostgresProposalStore(intel_pool)
    detail = await store.get_proposal(proposal)
    assert detail is not None
    (related,) = detail["related_events"]
    assert related["event_id"] == event_id and related["direction"] == "threat"
    assert related["title"] == "Live breach" and related["tags"] == ["instagram"]
    assert set(related) == {
        "event_id",
        "direction",
        "kind",
        "title",
        "severity",
        "tags",
        "is_global",
        "starts_at",
        "expires_at",
        "proposal_id",
    }
    weight = await store.get_proposal(change)
    assert weight is not None and weight["related_events"] == []


# ── beyond the brief: what the six cases above leave to a mutant ────────────────────────────


async def test_an_attach_only_write_is_audited_and_a_fully_dropped_one_is_not(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    dismissed = await seed_threat_proposal(intel_pool, signal_ids=[old], status="rejected")
    store = PostgresProposalStore(intel_pool)
    kept_run = await _run(intel_pool)
    new = await seed_signal(intel_pool, run_id=kept_run, tags=("instagram",), publisher="b.example")
    kept = await _write(store, kept_run, attachments=[Attachment(pending, (new,))])
    assert kept.written == () and kept.superseded == () and kept.attached == (pending,)
    audit = await _scalar(
        intel_pool,
        "SELECT metadata FROM audit_log WHERE action = 'intel.proposals_written'"
        " AND resource_id = %s",
        kept_run,
    )
    assert audit == {"written": [], "superseded": [], "attached": [str(pending)]}
    dropped_run = await _run(intel_pool)
    later = await seed_signal(
        intel_pool, run_id=dropped_run, tags=("instagram",), publisher="c.example"
    )
    dropped = await _write(store, dropped_run, attachments=[Attachment(dismissed, (later,))])
    assert dropped.attached == () and dropped.attach_dropped == 1
    assert await store.proposals_written(dropped_run)  # the write still commits, as recorded
    audited = await _scalar(
        intel_pool,
        "SELECT count(*) FROM audit_log WHERE action = 'intel.proposals_written'"
        " AND resource_id = %s",
        dropped_run,
    )
    assert audited == 0  # nothing written, superseded or attached: nothing to record


async def _lock_waiter_appeared(pool: AsyncConnectionPool, *, seconds: float = 5.0) -> bool:
    """True once some backend of this database is blocked on a row lock in a FOR UPDATE."""
    for _ in range(int(seconds / 0.05)):
        waiting = await _scalar(
            pool,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND wait_event_type = 'Lock' AND strpos(query, 'FOR UPDATE') > 0",
        )
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_the_attachment_recheck_waits_for_the_row_lock_a_decision_holds(
    intel_pool: AsyncConnectionPool, intel_db: str
) -> None:
    """spec §4.3, "re-checked under lock". A decision in flight holds the proposal's row; the
    write must wait for it and then see the answer, not attach to a proposal somebody has just
    dismissed. A plain status check without the lock passes every other test and loses this race.
    """
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    run_id = await _run(intel_pool)
    new = await seed_signal(intel_pool, run_id=run_id, tags=("instagram",), publisher="b.example")
    store = PostgresProposalStore(intel_pool)
    async with await AsyncConnection.connect(intel_db) as decision:
        await decision.execute(
            "SELECT 1 FROM intel_proposals WHERE proposal_id = %s FOR UPDATE", (pending,)
        )
        write = asyncio.create_task(
            _write(store, run_id, attachments=[Attachment(pending, (new,))])
        )
        blocked = await _lock_waiter_appeared(intel_pool)
        await decision.execute(
            "UPDATE intel_proposals SET status = 'rejected', decided_by = 'ann',"
            " decided_at = now(), decision_reason = 'not this' WHERE proposal_id = %s",
            (pending,),
        )
        await decision.commit()
        result = await asyncio.wait_for(write, timeout=10)
    assert blocked, "write_generated never waited on the proposal's row lock"
    assert result.attached == () and result.attach_dropped == 1
    links = await _scalar(
        intel_pool, "SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = %s", pending
    )
    assert links == 1  # the dismissed proposal kept only the evidence it was dismissed on


@pytest.fixture
async def intel_rw_pool(intel_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose sessions run AS ``intel_rw``, the role the intel worker holds. Every other
    test here connects as the superuser, which hides a missing grant (the 0035 trap)."""
    sep = "&" if "?" in intel_db else "?"
    pool = make_async_pool(f"{intel_db}{sep}options=-c%20role%3Dintel_rw", min_size=1, max_size=2)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def test_the_event_reads_and_the_attachment_write_need_no_grant_intel_rw_lacks(
    intel_pool: AsyncConnectionPool, intel_rw_pool: AsyncConnectionPool
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    applied = await seed_threat_proposal(
        intel_pool,
        signal_ids=[old],
        status="applied",
        decided={**THREAT_SUGGESTED, "tags": ["instagram"]},
    )
    await seed_threat_event(intel_pool, proposal_id=applied, title="live")
    run_id = await _run(intel_pool)
    new = await seed_signal(intel_pool, run_id=run_id, tags=("instagram",), publisher="b.example")
    assert await _scalar(intel_rw_pool, "SELECT current_user") == "intel_rw"
    store = PostgresProposalStore(intel_rw_pool)
    open_events = await store.pending_event_proposals(tags=["instagram"], limit=40)
    assert [p.proposal_id for p in open_events] == [pending]
    live = await store.active_threat_events(tags=["instagram"], limit=40)
    assert [e.title for e in live] == ["live"]
    assert [s.signal_id for s in await store.signals_by_id([old, new])] == [old, new]
    result = await _write(store, run_id, _threat(new), attachments=[Attachment(pending, (new,))])
    assert len(result.written) == 1 and result.attached == (pending,)
    detail = await store.get_proposal(pending)
    assert detail is not None and [r["title"] for r in detail["related_events"]] == ["live"]


async def test_related_events_leave_out_the_proposals_own_event_and_other_tags(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    applied = await seed_threat_proposal(
        intel_pool,
        signal_ids=[sid],
        status="applied",
        decided={**THREAT_SUGGESTED, "tags": ["instagram"]},
    )
    await seed_threat_event(intel_pool, proposal_id=applied, title="its own")
    other = await seed_threat_event(intel_pool, title="someone else's")
    await seed_threat_event(intel_pool, title="different tag", tags=("linkedin",))
    detail = await PostgresProposalStore(intel_pool).get_proposal(applied)
    assert detail is not None
    assert [r["event_id"] for r in detail["related_events"]] == [other]


async def test_the_event_reads_are_newest_first_and_bounded(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    older = await seed_threat_proposal(
        intel_pool, signal_ids=[sid], created_at=NOW - timedelta(hours=2)
    )
    newer = await seed_threat_proposal(
        intel_pool, signal_ids=[sid], created_at=NOW - timedelta(hours=1)
    )
    for title in ("first", "second", "third"):
        await seed_threat_event(intel_pool, title=title)
    store = PostgresProposalStore(intel_pool)
    both = await store.pending_event_proposals(tags=["instagram"], limit=40)
    assert [p.proposal_id for p in both] == [newer, older]
    one = await store.pending_event_proposals(tags=["instagram"], limit=1)
    assert [p.proposal_id for p in one] == [newer]
    live = await store.active_threat_events(tags=["instagram"], limit=2)
    assert [e.title for e in live] == ["third", "second"]
    assert await store.active_threat_events(tags=[], limit=40) == []


async def test_pending_event_proposals_read_other_event_kinds_and_refuse_a_boolean_severity(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool, tags=("instagram",))
    protection = await seed_proposal(
        intel_pool,
        signal_ids=[sid],
        kind="protection_event",
        target={"tags": ["instagram"], "is_global": False},
        suggested={"title": "Two-factor everywhere", "strength": 2, "review_in_days": 30},
    )
    odd = await seed_proposal(
        intel_pool,
        signal_ids=[sid],
        kind="threat_event",
        target={"tags": ["instagram"]},
        suggested={"title": "odd", "severity": True},
    )
    found = {
        p.proposal_id: p
        for p in await PostgresProposalStore(intel_pool).pending_event_proposals(
            tags=["instagram"], limit=40
        )
    }
    assert found[protection].kind == "protection_event" and found[protection].severity is None
    assert found[protection].title == "Two-factor everywhere"
    assert found[odd].severity is None  # True is an int in Python and not a severity


async def test_active_subject_signals_are_the_windows_active_signals_naming_a_subject(
    intel_pool: AsyncConnectionPool,
) -> None:
    first = await seed_signal(intel_pool, subjects=("Bumble",))
    second = await seed_signal(intel_pool, subjects=("Hinge",))
    await seed_signal(intel_pool, subjects=("Match",), created_at=NOW - timedelta(days=120))
    await seed_signal(intel_pool, tags=("instagram",))  # names no subject
    gone = await seed_signal(intel_pool, subjects=("Feeld",))
    await PostgresEvidenceStore(intel_pool).retract_signal(gone, operator="a", reason="wrong")
    since = NOW - timedelta(days=90)
    async with intel_pool.connection() as conn:
        found = await active_subject_signals(conn, since=since, limit=10)
        newest = await active_subject_signals(conn, since=since, limit=1)
    assert [s.signal_id for s in found] == [second, first]
    assert [s.signal_id for s in newest] == [second]
    assert all(s.document_key is not None for s in found)


def _protection(signal_id: UUID) -> NewProposal:
    return NewProposal(
        "protection_event",
        {"tags": ["instagram"], "is_global": False},
        dict(PROTECTION_SUGGESTED),
        "because",
        (signal_id,),
    )


async def test_a_protection_proposal_is_written_pending_and_supersedes_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    store = PostgresProposalStore(intel_pool)
    first = await _write(store, await _run(intel_pool), _protection(sid))
    second = await _write(store, await _run(intel_pool), _protection(sid))
    assert second.superseded == () and len(second.written) == 1
    rows = await store.list_proposals(
        statuses=["pending"], kinds=["protection_event"], cursor=None, limit=10
    )
    assert {r["proposal_id"] for r in rows} == {*first.written, *second.written}
    assert all(r["against_release_no"] is None for r in rows)
    assert rows[0]["suggested"] == PROTECTION_SUGGESTED


async def test_active_protection_events_are_live_on_the_tags_or_global_with_their_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool, tags=("instagram",))
    applied = await seed_protection_proposal(
        intel_pool, signal_ids=[sid], status="applied", decided=protection_decided()
    )
    live = await seed_protection_event(intel_pool, proposal_id=applied, title="live")
    await seed_protection_event(intel_pool, tags=(), is_global=True, title="everyone")
    await seed_protection_event(intel_pool, title="retracted", status="retracted")
    await seed_protection_event(intel_pool, title="lapsed", starts_in_days=-100, ends_in_days=-1)
    await seed_protection_event(intel_pool, title="scheduled", starts_in_days=10, ends_in_days=100)
    await seed_protection_event(intel_pool, title="other tag", tags=("linkedin",))
    store = PostgresProposalStore(intel_pool)
    events = await store.active_protection_events(tags=["instagram"], limit=40)
    assert {e.title for e in events} == {"live", "everyone"}
    (approved,) = [e for e in events if e.event_id == live]
    assert approved.signal_ids == (sid,) and approved.proposal_id == applied
    assert approved.strength == 2 and approved.renews_event_id is None
    assert [e.title for e in await store.active_protection_events(tags=[], limit=40)] == [
        "everyone"
    ]


async def test_each_event_kind_is_related_to_the_live_events_of_its_own_direction(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    protection = await seed_protection_proposal(intel_pool, signal_ids=[sid])
    threat = await seed_threat_proposal(intel_pool, signal_ids=[sid])
    credit = await seed_protection_event(intel_pool, title="Live opt-out")
    incident = await seed_threat_event(intel_pool, title="Live breach")
    store = PostgresProposalStore(intel_pool)
    detail = await store.get_proposal(protection)
    assert detail is not None
    (related,) = detail["related_events"]
    assert related["event_id"] == credit and related["direction"] == "protection"
    assert set(related) == {
        "event_id",
        "direction",
        "kind",
        "title",
        "strength",
        "tags",
        "is_global",
        "starts_at",
        "review_by",
        "proposal_id",
    }
    threat_detail = await store.get_proposal(threat)
    assert threat_detail is not None
    assert [e["event_id"] for e in threat_detail["related_events"]] == [incident]
