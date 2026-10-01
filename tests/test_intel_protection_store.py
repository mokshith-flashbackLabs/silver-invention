"""protection_events: the list with each credit's renewal state, and the terminal retraction
(spec §3.7, §4.7, §4.8) against a real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.proposal_models import Decided, DecisionRefused
from imageshield.intel.protection_store import PostgresProtectionStore, ProtectionRetraction
from tests.intel_fakes import (
    seed_protection_event,
    seed_protection_proposal,
    seed_quiz_vocabulary,
    seed_signal,
)


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _row(pool: AsyncConnectionPool, query: str, *params: Any) -> tuple[Any, ...]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return tuple(row)


async def _listed(pool: AsyncConnectionPool) -> dict[UUID, dict[str, Any]]:
    rows = await PostgresProtectionStore(pool).list_events(statuses=None, cursor=None, limit=200)
    return {r["event_id"]: r for r in rows}


async def test_the_list_names_each_credits_state_and_whether_its_renewal_is_due(
    intel_pool: AsyncConnectionPool,
) -> None:
    due = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=20)
    later = await seed_protection_event(intel_pool, starts_in_days=-10, ends_in_days=90)
    lapsed = await seed_protection_event(intel_pool, starts_in_days=-100, ends_in_days=-1)
    retracted = await seed_protection_event(intel_pool, status="retracted")
    renewed = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=10)
    scheduled = await seed_protection_event(
        intel_pool, renews=renewed, starts_in_days=10, ends_in_days=190
    )
    rows = await _listed(intel_pool)
    assert {e: (rows[e]["state"], rows[e]["renewal_due"]) for e in rows} == {
        due: ("live", True),
        later: ("live", False),
        lapsed: ("lapsed", False),
        retracted: ("retracted", False),
        renewed: ("live", False),
        scheduled: ("scheduled", False),
    }
    assert rows[scheduled]["renews_event_id"] == renewed and rows[due]["renewal"] is None
    assert rows[due]["body"] == "" and rows[due]["strength"] == 2
    assert rows[retracted]["retracted_by"] == "seed-op"


async def test_the_list_filters_by_status_and_pages_newest_first(
    intel_pool: AsyncConnectionPool,
) -> None:
    for n in range(3):
        await seed_protection_event(intel_pool, title=f"live {n}")
    await seed_protection_event(intel_pool, title="gone", status="retracted")
    store = PostgresProtectionStore(intel_pool)
    active = await store.list_events(statuses=["active"], cursor=None, limit=50)
    assert {r["title"] for r in active} == {"live 0", "live 1", "live 2"}
    first = await store.list_events(statuses=None, cursor=None, limit=2)
    rest = await store.list_events(
        statuses=None, cursor=(first[-1]["created_at"], first[-1]["event_id"]), limit=50
    )
    assert len(first) == 2 and len(rest) == 2
    assert [r["title"] for r in first] == ["gone", "live 2"]
    assert not {r["event_id"] for r in first} & {r["event_id"] for r in rest}


async def test_the_list_carries_the_latest_renewal_check_and_its_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    event = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=20)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by, status, outcome, completed_at)"
            " VALUES ('renewal_check', jsonb_build_object('event_id', %s::text), 'schedule',"
            " 'completed', '{\"renewal_evidence_gone\": 1}', now())",
            (str(event),),
        )
    renewal = (await _listed(intel_pool))[event]["renewal"]
    assert renewal["result"] == "evidence_gone" and renewal["run_status"] == "completed"
    assert renewal["proposal_id"] is None
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_protection_proposal(intel_pool, signal_ids=[sid], renews=event)
    row = (await _listed(intel_pool))[event]
    assert row["renewal"]["proposal_id"] == pending
    assert row["renewal"]["proposal_status"] == "pending"
    assert row["renewal_due"] is True  # nothing continues it until a renewal is approved


async def test_retraction_is_terminal_named_audited_and_a_repeat_writes_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    event = await seed_protection_event(intel_pool)
    store = PostgresProtectionStore(intel_pool)
    retraction = await store.retract(event, operator="ann", reason="the feature was withdrawn")
    assert retraction == ProtectionRetraction(event, (), ())
    assert await _row(
        intel_pool,
        "SELECT status, retracted_by, retract_reason FROM protection_events WHERE event_id = %s",
        event,
    ) == ("retracted", "ann", "the feature was withdrawn")
    again = await store.retract(event, operator="bob", reason="again")
    assert again == ProtectionRetraction(event, (), (), already_retracted=True)
    assert await store.retract(uuid4(), operator="bob", reason="nothing there") is None
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.protection_retracted'"
        )
        == 1
    )
    metadata = await _scalar(
        intel_pool, "SELECT metadata FROM audit_log WHERE action = 'intel.protection_retracted'"
    )
    assert metadata["operator"] == "ann" and metadata["also_retracted"] == []


async def test_retracting_a_credit_takes_its_unstarted_renewal_and_pending_renewal_with_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 1. A renewal approved but not started would bring the credit back at the old
    review date; a pending renewal would still read approvable. Neither survives the retraction.
    A renewal that has already started is its own live credit and is untouched."""
    old = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=20)
    scheduled = await seed_protection_event(
        intel_pool, renews=old, starts_in_days=20, ends_in_days=200
    )
    other = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=20)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_protection_proposal(intel_pool, signal_ids=[sid], renews=other)
    store = PostgresProtectionStore(intel_pool)
    first = await store.retract(old, operator="ann", reason="withdrawn")
    assert first == ProtectionRetraction(old, (scheduled,), ())
    assert await _row(
        intel_pool,
        "SELECT status, retracted_by FROM protection_events WHERE event_id = %s",
        scheduled,
    ) == ("retracted", "ann")
    second = await store.retract(other, operator="ann", reason="withdrawn")
    assert second == ProtectionRetraction(other, (), (pending,))
    status, decided_by, decision_reason = await _row(
        intel_pool,
        "SELECT status, decided_by, decision_reason FROM intel_proposals WHERE proposal_id = %s",
        pending,
    )
    assert (status, decided_by) == ("rejected", "ann")
    assert decision_reason.startswith("The protection this renews was retracted")
    lapsed = await seed_protection_event(intel_pool, starts_in_days=-200, ends_in_days=-1)
    started = await seed_protection_event(
        intel_pool, renews=lapsed, starts_in_days=-1, ends_in_days=179
    )
    third = await store.retract(lapsed, operator="ann", reason="tidying up")
    assert third == ProtectionRetraction(lapsed, (), ())
    assert (
        await _scalar(
            intel_pool, "SELECT status FROM protection_events WHERE event_id = %s", started
        )
        == "active"
    )


# -- the lock order, carried from Task 6 -----------------------------------------------------
#
# An approval locks its PROPOSAL, then the credit a renewal continues (intel/decisions.py). The
# retraction locks the credit's pending renewal proposals BEFORE the credit, the same order, so
# the two can meet without deadlocking. Each test below FORCES one interleaving rather than
# hoping ``gather`` produces it: a holder pauses one side while it holds its first lock, the
# other side is started and seen to wait, then the holder lets go. With the retraction's locks
# taken in the other order, the first test deadlocks (Postgres aborts one side with
# DeadlockDetected after deadlock_timeout).


async def _lock_waiters(pool: AsyncConnectionPool) -> int:
    count: int = await _scalar(
        pool,
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_database() AND wait_event_type = 'Lock'",
    )
    return count


async def _until_waiting(pool: AsyncConnectionPool, sessions: int) -> None:
    for _ in range(200):
        if await _lock_waiters(pool) >= sessions:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {sessions} session(s) waiting on a lock")


async def _row_is_locked(pool: AsyncConnectionPool, query: str, *params: Any) -> bool:
    """True while another transaction holds the row ``query`` selects FOR UPDATE. The probe's
    own lock, when it gets one, is rolled back at once."""
    async with pool.connection() as conn:
        try:
            async with conn.transaction(force_rollback=True):
                await conn.execute(f"{query} FOR UPDATE NOWAIT", params)
        except psycopg.errors.LockNotAvailable:
            return True
    return False


_PROPOSAL_ROW = "SELECT 1 FROM intel_proposals WHERE proposal_id = %s"
_CREDIT_ROW = "SELECT 1 FROM protection_events WHERE event_id = %s"


async def _approve(pool: AsyncConnectionPool, proposal_id: UUID) -> Decided:
    return await PostgresDecisionStore(pool).decide(
        proposal_id,
        decision="approved",
        values=None,
        reason="re-checked the sources",
        operator="bob",
        applies_regardless_of_location=True,
    )


async def _due_credit_with_a_pending_renewal(pool: AsyncConnectionPool) -> tuple[UUID, UUID]:
    await seed_quiz_vocabulary(pool)
    credit = await seed_protection_event(pool, starts_in_days=-160, ends_in_days=20)
    sid = await seed_signal(pool, tags=("instagram",))  # listed: corroborated alone
    renewal = await seed_protection_proposal(pool, signal_ids=[sid], renews=credit)
    return credit, renewal


async def test_a_retraction_meeting_an_approval_of_its_renewal_waits_and_takes_the_renewal(
    intel_pool: AsyncConnectionPool, intel_db: str
) -> None:
    """The approval holds its renewal proposal and has not yet reached the credit. The
    retraction waits on that proposal WITHOUT having touched the credit, so the approval
    finishes, and its renewal -- approved, not started -- goes with the credit. Neither side
    deadlocks."""
    credit, renewal = await _due_credit_with_a_pending_renewal(intel_pool)
    store = PostgresProtectionStore(intel_pool)
    async with await psycopg.AsyncConnection.connect(intel_db, autocommit=True) as holder:
        async with holder.transaction():
            # The decision reads the vocabulary straight after locking its proposal.
            await holder.execute("LOCK TABLE intel_vocabulary IN ACCESS EXCLUSIVE MODE")
            approval = asyncio.create_task(_approve(intel_pool, renewal))
            await _until_waiting(intel_pool, 1)
            assert await _row_is_locked(intel_pool, _PROPOSAL_ROW, renewal)
            retraction = asyncio.create_task(
                store.retract(credit, operator="ann", reason="withdrawn")
            )
            await _until_waiting(intel_pool, 2)
            assert not await _row_is_locked(intel_pool, _CREDIT_ROW, credit)
        results = await asyncio.wait_for(
            asyncio.gather(approval, retraction, return_exceptions=True), timeout=30
        )
    assert not [r for r in results if isinstance(r, BaseException)], results
    decided, retracted = results
    assert isinstance(decided, Decided) and decided.status == "applied"
    assert decided.applied_ref is not None
    assert retracted == ProtectionRetraction(credit, (UUID(decided.applied_ref),), ())
    assert await _scalar(
        intel_pool,
        "SELECT array_agg(status ORDER BY created_at) FROM protection_events"
        " WHERE event_id = ANY(%s::uuid[])",
        [credit, UUID(decided.applied_ref)],
    ) == ["retracted", "retracted"]


async def test_an_approval_meeting_a_retraction_of_its_credit_is_refused_cleanly(
    intel_pool: AsyncConnectionPool, intel_db: str
) -> None:
    """The retraction holds the pending renewal proposal and the credit, uncommitted. The
    approval waits on the proposal, then finds it rejected: one clean proposal_not_pending, no
    credit. Neither side deadlocks."""
    credit, renewal = await _due_credit_with_a_pending_renewal(intel_pool)
    store = PostgresProtectionStore(intel_pool)
    async with await psycopg.AsyncConnection.connect(intel_db, autocommit=True) as holder:
        async with holder.transaction():
            # The retraction's audit row is its last write: it pauses holding every row lock.
            await holder.execute("LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE")
            retraction = asyncio.create_task(
                store.retract(credit, operator="ann", reason="withdrawn")
            )
            await _until_waiting(intel_pool, 1)
            assert await _row_is_locked(intel_pool, _PROPOSAL_ROW, renewal)
            assert await _row_is_locked(intel_pool, _CREDIT_ROW, credit)
            approval = asyncio.create_task(_approve(intel_pool, renewal))
            await _until_waiting(intel_pool, 2)
        results = await asyncio.wait_for(
            asyncio.gather(retraction, approval, return_exceptions=True), timeout=30
        )
    retracted, refused = results
    assert retracted == ProtectionRetraction(credit, (), (renewal,))
    assert isinstance(refused, DecisionRefused), results
    assert refused.code == "proposal_not_pending"
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM protection_events WHERE proposal_id = %s", renewal
        )
        == 0
    )


async def test_two_simultaneous_retractions_write_one_retraction(
    intel_pool: AsyncConnectionPool,
) -> None:
    credit, renewal = await _due_credit_with_a_pending_renewal(intel_pool)
    store = PostgresProtectionStore(intel_pool)
    results = await asyncio.gather(
        store.retract(credit, operator="ann", reason="withdrawn"),
        store.retract(credit, operator="bob", reason="withdrawn"),
    )
    assert sorted(results, key=lambda r: r is not None and r.already_retracted) == [
        ProtectionRetraction(credit, (), (renewal,)),
        ProtectionRetraction(credit, (), (), already_retracted=True),
    ]
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM audit_log WHERE action = 'intel.protection_retracted'"
        )
        == 1
    )


@pytest.fixture
async def intel_rw_pool(intel_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose sessions run AS ``intel_rw``. The tests above connect as the superuser,
    which hides a missing grant (the 0035 trap)."""
    sep = "&" if "?" in intel_db else "?"
    pool = make_async_pool(f"{intel_db}{sep}options=-c%20role%3Dintel_rw", min_size=1, max_size=2)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def test_the_list_and_the_retraction_need_no_grant_intel_rw_lacks(
    intel_pool: AsyncConnectionPool, intel_rw_pool: AsyncConnectionPool
) -> None:
    """The list's renewal joins and the whole retraction -- the proposal locks, both credit
    updates, the proposal rejection and the audit row -- run AS intel_rw."""
    credit, renewal = await _due_credit_with_a_pending_renewal(intel_pool)
    other = await seed_protection_event(intel_pool, starts_in_days=-160, ends_in_days=20)
    scheduled = await seed_protection_event(
        intel_pool, renews=other, starts_in_days=20, ends_in_days=200
    )
    assert await _scalar(intel_rw_pool, "SELECT current_user") == "intel_rw"
    store = PostgresProtectionStore(intel_rw_pool)
    rows = {r["event_id"]: r for r in await store.list_events(statuses=None, cursor=None, limit=50)}
    assert rows[credit]["renewal"]["proposal_id"] == renewal
    assert await store.retract(credit, operator="ann", reason="withdrawn") == (
        ProtectionRetraction(credit, (), (renewal,))
    )
    assert await store.retract(other, operator="ann", reason="withdrawn") == (
        ProtectionRetraction(other, (scheduled,), ())
    )
