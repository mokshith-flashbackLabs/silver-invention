"""PostgresReviewStore against real Postgres (Task 15).

Same convention as ``tests/test_confirm_store.py``: own ``down --all`` + ``up``
arrange step, direct SQL for fixtures, and pending tasks seeded through
``PostgresConfirmStore.record_triage`` — the real producer — rather than
inserted by hand, so these tests exercise the same shape the confirm worker
actually writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.confirm.store import PostgresConfirmStore
from imageshield.db.connection import make_async_pool
from imageshield.review.store import (
    REVIEW_DECIDED_ACTION,
    SUBJECT_DECIDED_ACTION,
    PostgresReviewStore,
)
from imageshield.types import UserRef
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def stores(
    migrated_db: str,
) -> AsyncIterator[tuple[PostgresConfirmStore, PostgresReviewStore]]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresConfirmStore(pool), PostgresReviewStore(pool)
    finally:
        await pool.close()


def _user() -> UserRef:
    return UserRef(uuid4())


def _subject(conn: psycopg.Connection[Any], user_ref: UserRef) -> None:
    conn.execute(
        "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
        " VALUES (%s, true, 'adult')",
        (user_ref,),
    )


def _seed(conn: psycopg.Connection[Any], user_ref: UserRef) -> UUID:
    row = conn.execute(
        "INSERT INTO search_seeds (user_ref, seed_kind, source_object_ref)"
        " VALUES (%s, 'user_supplied', %s) RETURNING seed_id",
        (user_ref, f"photo/{uuid4().hex}"),
    ).fetchone()
    assert row is not None
    seed_id: UUID = row[0]
    return seed_id


def _run(conn: psycopg.Connection[Any], user_ref: UserRef, seed_id: UUID) -> UUID:
    row = conn.execute(
        "INSERT INTO search_runs (seed_id, user_ref, providers_attempted, threshold_config,"
        " seed_url, status, completed_at)"
        " VALUES (%s, %s, %s, '{}'::jsonb, 'https://s3.test/x', 'completed', now())"
        " RETURNING run_id",
        (seed_id, user_ref, ["hive"]),
    ).fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    run_id: UUID,
    *,
    source_domain: str = "example.test",
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://{source_domain}/{uuid4().hex}"
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, %s, %s)",
        (url_hash, url, source_domain, url),
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url)"
        " VALUES (%s, %s, %s, %s) RETURNING infringement_id",
        (user_ref, url_hash, url, f"{url}.jpg"),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    conn.execute(
        "INSERT INTO attestations (infringement_id, provider_id, score_kind, provider_score,"
        " score_version, band, last_run_id)"
        " VALUES (%s, 'hive', 'numeric', %s, 'v1', 'review', %s)",
        (infringement_id, "0.80", run_id),
    )
    return infringement_id


async def _seeded_infringement(
    migrated_db: str,
    confirm_store: PostgresConfirmStore,
    *,
    severity: str,
    source_domain: str = "example.test",
) -> tuple[UserRef, UUID]:
    """One infringement plus a pending review task, produced the real way:
    through ``record_triage`` rather than a hand-written ``review_tasks``
    insert."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed_id = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed_id)
        infringement_id = _infringement(
            conn, user_ref, run_id, source_domain=source_domain
        )
    await confirm_store.record_triage(
        infringement_id,
        severity=severity,
        phash=42,
        face_match_score=91.25,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 90.0}],
        triage={"face_match_score": 91.25, "best_face_bbox": {"left": 0.1, "top": 0.2}},
    )
    return user_ref, infringement_id


def _row(migrated_db: str, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _rows(migrated_db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


# ── next_task ────────────────────────────────────────────────────────────


async def test_next_task_returns_highest_priority_pending_row(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    # benign_copy seeded FIRST (would win on created_at alone), ncii_suspected
    # SECOND -- the queue order must still pick ncii_suspected, proving the
    # ordering is by severity rank, not insertion order.
    await _seeded_infringement(migrated_db, confirm_store, severity="benign_copy")
    _user_ref, ncii_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )

    task = await review_store.next_task()

    assert task is not None
    assert task["infringement_id"] == ncii_id
    assert task["severity"] == "ncii_suspected"
    assert task["image_url"] is not None
    assert task["page_url"] is not None
    assert task["face_match_score"] == 91.25
    assert task["source_domain"] == "example.test"
    assert task["triage"]["best_face_bbox"] == {"left": 0.1, "top": 0.2}


async def test_next_task_is_none_when_the_queue_is_empty(
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    _confirm_store, review_store = stores
    assert await review_store.next_task() is None


async def test_next_task_never_returns_a_quarantined_row(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed_id = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed_id)
        infringement_id = _infringement(conn, user_ref, run_id)
    await confirm_store.record_quarantine(
        infringement_id, phash=1, moderation_labels=[], min_age_low=10.0
    )

    assert await review_store.next_task() is None


# ── queue_depth ──────────────────────────────────────────────────────────


async def test_queue_depth_counts_per_severity(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    await _seeded_infringement(migrated_db, confirm_store, severity="ncii_suspected")
    await _seeded_infringement(migrated_db, confirm_store, severity="ncii_suspected")
    await _seeded_infringement(migrated_db, confirm_store, severity="benign_copy")

    depths = await review_store.queue_depth()

    assert depths["ncii_suspected"] == 2
    assert depths["benign_copy"] == 1
    assert depths["explicit_unmatched"] == 0
    assert depths["unassessed"] == 0
    assert depths["likely_not_subject"] == 0


# ── decide ───────────────────────────────────────────────────────────────


async def test_decide_confirms_sets_infringement_state_severity_and_decided_by(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    task = await review_store.next_task()
    assert task is not None

    outcome = await review_store.decide(
        task["task_id"], decision="confirmed", operator="alice", severity="ncii_suspected"
    )

    assert outcome is not None
    assert outcome.infringement_id == infringement_id
    assert outcome.decision == "confirmed"
    assert outcome.severity == "ncii_suspected"

    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity, confirm_decided_by, confirm_decided_at"
        " FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "confirmed"
    assert infr["severity"] == "ncii_suspected"
    assert infr["confirm_decided_by"] == "alice"
    assert infr["confirm_decided_at"] is not None

    task_row = _row(
        migrated_db,
        "SELECT status, decision, decided_by, decided_at FROM review_tasks"
        " WHERE task_id = %s",
        (task["task_id"],),
    )
    assert task_row["status"] == "decided"
    assert task_row["decision"] == "confirmed"
    assert task_row["decided_by"] == "alice"
    assert task_row["decided_at"] is not None

    audit = _row(
        migrated_db,
        "SELECT action, subject_ref, resource_id, metadata FROM audit_log"
        " WHERE resource_id = %s AND actor_type = 'operator'",
        (infringement_id,),
    )
    assert audit["action"] == REVIEW_DECIDED_ACTION == "review.decided"
    assert audit["metadata"] == {
        "decision": "confirmed",
        "severity": "ncii_suspected",
        "operator": "alice",
    }


async def test_decide_without_severity_override_leaves_severity_untouched(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    task = await review_store.next_task()
    assert task is not None

    outcome = await review_store.decide(
        task["task_id"], decision="rejected", operator="bob", severity=None
    )

    assert outcome is not None
    assert outcome.decision == "rejected"
    assert outcome.severity == "benign_copy"
    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "rejected"
    assert infr["severity"] == "benign_copy"


async def test_deciding_an_already_decided_task_returns_none(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    await _seeded_infringement(migrated_db, confirm_store, severity="benign_copy")
    task = await review_store.next_task()
    assert task is not None

    first = await review_store.decide(
        task["task_id"], decision="confirmed", operator="alice", severity=None
    )
    assert first is not None

    second = await review_store.decide(
        task["task_id"], decision="rejected", operator="carol", severity=None
    )
    assert second is None


async def test_deciding_an_unknown_task_returns_none(
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    _confirm_store, review_store = stores
    assert await review_store.decide(
        uuid4(), decision="confirmed", operator="alice", severity=None
    ) is None


async def test_decide_uncertain_keeps_the_task_pending(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    task = await review_store.next_task()
    assert task is not None

    outcome = await review_store.decide(
        task["task_id"], decision="uncertain", operator="dana", severity=None
    )

    assert outcome is not None
    assert outcome.decision == "uncertain"
    assert outcome.severity is None
    assert outcome.infringement_id == infringement_id

    # The task is untouched -- still pending, no decision recorded.
    task_row = _row(
        migrated_db,
        "SELECT status, decision, decided_by, decided_at FROM review_tasks"
        " WHERE task_id = %s",
        (task["task_id"],),
    )
    assert task_row["status"] == "pending"
    assert task_row["decision"] is None
    assert task_row["decided_by"] is None
    assert task_row["decided_at"] is None

    # next_task still returns the SAME task -- nothing timed out or auto-promoted.
    still_next = await review_store.next_task()
    assert still_next is not None
    assert still_next["task_id"] == task["task_id"]

    # The infringement itself is untouched.
    infr = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "machine_triaged"
    assert infr["confirm_decided_by"] is None

    audit = _row(
        migrated_db,
        "SELECT action, metadata FROM audit_log"
        " WHERE resource_id = %s AND actor_type = 'operator'",
        (infringement_id,),
    )
    assert audit["action"] == REVIEW_DECIDED_ACTION
    assert audit["metadata"] == {"decision": "uncertain", "operator": "dana"}


async def test_decide_never_trips_the_infringements_confirmed_needs_human_check(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """The CHECK itself (infringements_confirmed_needs_human, added in 0021)
    raises on a direct SQL UPDATE that sets confirm_state='confirmed' with no
    decided_by/decided_at. Here we assert the STORE's own path never trips
    it -- decide() always sets both in the same statement that sets
    confirm_state."""
    confirm_store, review_store = stores
    await _seeded_infringement(migrated_db, confirm_store, severity="benign_copy")
    task = await review_store.next_task()
    assert task is not None

    # No exception -- the store's UPDATE always carries confirm_decided_by
    # and confirm_decided_at alongside confirm_state.
    outcome = await review_store.decide(
        task["task_id"], decision="confirmed", operator="erin", severity=None
    )
    assert outcome is not None

    # And direct proof the CHECK is live in this schema at all: a bare UPDATE
    # with no decided_by raises CheckViolation.
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        psycopg.connect(migrated_db, autocommit=True) as conn,
    ):
        conn.execute(
            "UPDATE infringements SET confirm_state = 'confirmed',"
            " confirm_decided_by = NULL, confirm_decided_at = NULL"
            " WHERE infringement_id = %s",
            (outcome.infringement_id,),
        )


# ── subject_decide (spec 2026-08-21 §5) ─────────────────────────────────────


async def test_subject_confirm_writes_the_transition_and_audit(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy", source_domain="salon.example"
    )

    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    assert outcome is not None
    assert outcome.outcome == "decided"
    assert outcome.decision == "confirmed"
    assert outcome.severity == "benign_copy"  # machine severity untouched
    infringement = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by, confirm_decided_at, severity, status"
        " FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infringement["confirm_state"] == "confirmed"
    assert infringement["confirm_decided_by"] == "subject"
    assert infringement["confirm_decided_at"] is not None
    assert infringement["severity"] == "benign_copy"
    assert infringement["status"] == "new"  # confirming does not retire the hit
    task = _row(
        migrated_db,
        "SELECT status, decision, decided_by FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task == {"status": "decided", "decision": "confirmed", "decided_by": "subject"}
    audit = _row(
        migrated_db,
        "SELECT actor_type, subject_ref, resource_id, metadata FROM audit_log"
        " WHERE action = %s",
        (SUBJECT_DECIDED_ACTION,),
    )
    assert audit["actor_type"] == "subject"
    assert audit["subject_ref"] == user_ref
    assert audit["resource_id"] == infringement_id
    assert audit["metadata"] == {
        "decision": "confirmed",
        "severity": "benign_copy",
        "source_domain": "salon.example",
    }


async def test_subject_reject_retires_the_hit_from_their_counts(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="likely_not_subject"
    )

    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    assert outcome is not None and outcome.outcome == "decided"
    infringement = _row(
        migrated_db,
        "SELECT confirm_state, status FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infringement == {"confirm_state": "rejected", "status": "dismissed_not_me"}


async def test_repeat_of_the_same_decision_is_an_idempotent_replay(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    first = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )
    assert first is not None and first.outcome == "decided"

    replay = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    assert replay is not None
    assert replay.outcome == "replay"
    assert replay.severity == "benign_copy"
    audits = _rows(
        migrated_db,
        "SELECT audit_id FROM audit_log WHERE action = %s",
        (SUBJECT_DECIDED_ACTION,),
    )
    assert len(audits) == 1  # the replay wrote nothing


async def test_a_different_decision_after_the_first_is_a_conflict(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    conflict = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    assert conflict is not None
    assert conflict.outcome == "conflict"
    infringement = _row(
        migrated_db,
        "SELECT confirm_state FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infringement["confirm_state"] == "confirmed"  # nothing overwritten


async def test_a_subject_cannot_overturn_an_operator_decision(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE infringements SET confirm_state = 'rejected',"
            " confirm_decided_by = 'ops@imageshield', confirm_decided_at = now()"
            " WHERE infringement_id = %s",
            (infringement_id,),
        )

    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    assert outcome is not None
    assert outcome.outcome == "conflict"  # even the SAME decision: not theirs to replay


async def test_wrong_user_and_missing_hit_are_one_answer(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    _owner, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )

    not_yours = await review_store.subject_decide(
        infringement_id, user_ref=_user(), decision="confirmed"
    )
    not_there = await review_store.subject_decide(
        uuid4(), user_ref=_user(), decision="confirmed"
    )

    assert not_yours is None
    assert not_there is None


async def test_quarantined_is_invisible_to_the_subject(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE infringements SET confirm_state = 'quarantined'"
            " WHERE infringement_id = %s",
            (infringement_id,),
        )

    assert (
        await review_store.subject_decide(
            infringement_id, user_ref=user_ref, decision="confirmed"
        )
        is None
    )


async def test_deciding_an_untriaged_hit_works_without_a_review_task(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """A hit still 'unconfirmed' (never triaged) is decidable — the app falls
    back to domain + no preview and the subject can still answer."""
    _confirm_store, review_store = stores
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed_id = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed_id)
        infringement_id = _infringement(conn, user_ref, run_id)

    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    assert outcome is not None
    assert outcome.outcome == "decided"
    assert outcome.severity is None  # never triaged, so no machine severity


async def test_subject_decisions_feed_unpacks_newest_first(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    confirm_store, review_store = stores
    user_a, hit_a = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy", source_domain="a.example"
    )
    user_b, hit_b = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected", source_domain="b.example"
    )
    await review_store.subject_decide(hit_a, user_ref=user_a, decision="rejected")
    await review_store.subject_decide(hit_b, user_ref=user_b, decision="confirmed")

    feed = await review_store.subject_decisions(limit=10)

    assert [d["infringement_id"] for d in feed] == [hit_b, hit_a]
    newest = feed[0]
    assert newest["user_ref"] == user_b
    assert newest["decision"] == "confirmed"
    assert newest["severity"] == "ncii_suspected"
    assert newest["source_domain"] == "b.example"
    assert newest["occurred_at"] is not None


async def test_open_hits_lists_only_hits_awaiting_an_answer(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """Owner requirement 2026-08-21: the control room always sees THAT a
    person has a hit. Decided and quarantined rows leave the list."""
    confirm_store, review_store = stores
    user_open, hit_open = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy", source_domain="open.example"
    )
    user_decided, hit_decided = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    await review_store.subject_decide(
        hit_decided, user_ref=user_decided, decision="rejected"
    )
    _user_q, hit_quarantined = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE infringements SET confirm_state = 'quarantined'"
            " WHERE infringement_id = %s",
            (hit_quarantined,),
        )

    hits = await review_store.open_hits(limit=10)

    listed = {h["infringement_id"] for h in hits}
    assert hit_open in listed
    assert hit_decided not in listed
    assert hit_quarantined not in listed
    [row] = [h for h in hits if h["infringement_id"] == hit_open]
    assert row["user_ref"] == user_open
    assert row["confirm_state"] == "machine_triaged"
    assert row["severity"] == "benign_copy"
    assert row["source_domain"] == "open.example"


async def test_a_redelivered_machine_triage_cannot_clobber_a_subject_decision(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """SQS is at-least-once, and since the 2026-08-21 enqueue gate opened
    EVERY hit rides the queue -- so a triage message redelivered after the
    subject already answered is a normal event, not an exotic one. The
    confirm store's guarded UPDATE (confirm_state IN ('unconfirmed',
    'machine_triaged')) is what stops the machine overwriting a human, and
    its early return is what leaves the decided review_tasks row alone.
    """
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )
    assert outcome is not None and outcome.outcome == "decided"

    # The redelivery: same hit, same worker path, after the decision.
    await confirm_store.record_triage(
        infringement_id,
        severity="likely_not_subject",
        phash=99,
        face_match_score=None,
        moderation_labels=[],
        triage={"redelivered": True},
    )

    infringement = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by, severity FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infringement["confirm_state"] == "confirmed"
    assert infringement["confirm_decided_by"] == "subject"
    assert infringement["severity"] == "benign_copy"  # not re-classified
    task = _row(
        migrated_db,
        "SELECT status, decided_by, triage FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["status"] == "decided"
    assert task["decided_by"] == "subject"
    assert "redelivered" not in task["triage"]  # the decided task was not reopened
