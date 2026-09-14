"""PostgresReviewStore against real Postgres (Task 15).

Same convention as ``tests/test_confirm_store.py``: own ``down --all`` + ``up``
arrange step, direct SQL for fixtures, and pending tasks seeded through
``PostgresConfirmStore.record_triage`` — the real producer — rather than
inserted by hand, so these tests exercise the same shape the confirm worker
actually writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.confirm.models import AUTO_CONFIRM_DECIDED_BY
from imageshield.confirm.store import PostgresConfirmStore
from imageshield.db.connection import make_async_pool
from imageshield.review.store import (
    REVIEW_DECIDED_ACTION,
    REVIEW_VERDICT_ACTION,
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


async def _auto_confirmed_infringement(
    migrated_db: str, confirm_store: PostgresConfirmStore
) -> tuple[UserRef, UUID]:
    """One AUTO-CONFIRMED hit, produced the real way -- through
    ``record_auto_confirmed`` rather than a hand-written UPDATE, so these
    tests exercise the row shape the confirm worker actually writes."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed_id = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed_id)
        infringement_id = _infringement(conn, user_ref, run_id)
    await confirm_store.record_auto_confirmed(
        infringement_id,
        phash=77,
        face_match_score=98.0,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 96.0}],
        triage={"face_match_score": 98.0, "best_face_bbox": {"x": 0.1}},
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


async def test_a_subject_can_take_back_their_own_not_me(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """The undo, and the reason it exists (2026-09-01).

    Until now a second, different decision was a 409: "v1 has no re-decide,
    changes go through the team". That was wrong in the direction that matters.
    ``feedback.py`` already records why -- users reject TRUE positives, under
    distress, and it is common -- so the one answer people most need to take
    back was the one that needed a support ticket.
    """
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    undo = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    assert undo is not None
    assert undo.outcome == "decided"
    infringement = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infringement["confirm_state"] == "confirmed"
    assert infringement["confirm_decided_by"] == "subject"


async def test_the_undo_clears_the_dismissal_that_hid_the_hit(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """Reversing the DECISION has to reverse its side effect too.

    Rejecting sets status='dismissed_not_me', which is exactly the value the
    proxy's weekly-report close filters out of countable hits and the exposure
    count. Leave it behind and the user taps "actually this is me", watches the
    card change, and the number does not move -- the hit is confirmed and still
    hidden. It clears to 'new', where a FIRST-TIME confirm also leaves it, so
    the end state does not depend on how many answers came before.
    """
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )
    dismissed = _row(
        migrated_db,
        "SELECT status FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert dismissed["status"] == "dismissed_not_me"

    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    after = _row(
        migrated_db,
        "SELECT status FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert after["status"] == "new"


async def test_the_undo_moves_the_review_task_too(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """The task UPDATE used to be WHERE status = 'pending', so on a reversal it
    matched nothing and the queue kept asserting the answer the user had just
    changed."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )

    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    task = _row(
        migrated_db,
        "SELECT decision, decided_by FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["decision"] == "confirmed"
    assert task["decided_by"] == "subject"


async def test_the_undo_is_appended_to_the_audit_never_an_edit(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """Both answers stay in the record. Nothing about this feature deletes."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="confirmed"
    )

    audits = _rows(
        migrated_db,
        "SELECT metadata FROM audit_log WHERE action = %s ORDER BY audit_id",
        (SUBJECT_DECIDED_ACTION,),
    )
    assert [a["metadata"]["decision"] for a in audits] == ["rejected", "confirmed"]


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


# ── the auto-confirm override lane (2026-09-14, owner decision D3) ──────────
#
# Neither of these needed a code change: `decide` locks on `status = 'pending'`
# and its infringement UPDATE carries no `confirm_state` guard, and
# `subject_decide`'s conflict predicate is "decided by somebody who is not the
# subject". `record_auto_confirmed` leaves the task `pending` and writes a
# decider that is not `'subject'`, so both already fall out. They are pinned
# here because they are the two properties D3 and D4 rest on, and either could
# be broken by an innocuous-looking change to the other module.


async def test_an_operator_can_reject_an_auto_confirmed_hit(
    migrated_db: str, stores: tuple[PostgresConfirmStore, PostgresReviewStore]
) -> None:
    """The override D3 requires. The machine's confirm is not final: the task
    is still queued, and an operator's `rejected` overwrites both the state
    and the decider."""
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _auto_confirmed_infringement(
        migrated_db, confirm_store
    )

    task = await review_store.next_task()
    assert task is not None, "an auto-confirmed hit must still be queued for review"
    assert task["infringement_id"] == infringement_id
    assert task["severity"] == "ncii_suspected"

    outcome = await review_store.decide(
        task["task_id"], decision="rejected", operator="frank", severity=None
    )

    assert outcome is not None
    assert outcome.decision == "rejected"

    infr = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "rejected"
    assert infr["confirm_decided_by"] == "frank"
    assert infr["confirm_decided_by"] != AUTO_CONFIRM_DECIDED_BY

    task_row = _row(
        migrated_db,
        "SELECT status, decision, decided_by FROM review_tasks WHERE task_id = %s",
        (task["task_id"],),
    )
    assert task_row["status"] == "decided"
    assert task_row["decision"] == "rejected"
    assert task_row["decided_by"] == "frank"


@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
async def test_a_subject_cannot_overturn_a_machine_confirm(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
    decision: str,
) -> None:
    """D4's other half. The subject is never shown this hit and never asked
    about it -- but if a request for one reaches the decision endpoint anyway,
    it must not be taken. `'auto:nsfw'` is not `'subject'`, so the existing
    never-overturn-a-non-subject-decision predicate already answers conflict,
    which the route maps to 409 decision_conflict. Both values, because the
    "same answer replays" branch must not swallow `confirmed` either."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _auto_confirmed_infringement(
        migrated_db, confirm_store
    )

    outcome = await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision=decision
    )

    assert outcome is not None
    assert outcome.outcome == "conflict"

    infr = _row(
        migrated_db,
        "SELECT confirm_state, confirm_decided_by FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "confirmed"
    assert infr["confirm_decided_by"] == AUTO_CONFIRM_DECIDED_BY


# ── the reviewer feed (2026-09-14) ───────────────────────────────────────
#
# These read through PostgresReviewStore against the real database, so the
# keyset SQL, the LATERAL joins and the quarantine filter are all exercised
# as written rather than as remembered.


def _set_first_seen(migrated_db: str, infringement_id: UUID, when: datetime) -> None:
    """Move a hit in the feed's sort order. The keyset is
    (first_seen_at, infringement_id) DESC, and a fixture that inserts three
    rows inside one millisecond cannot prove paging is stable."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE infringements SET first_seen_at = %s WHERE infringement_id = %s",
            (when, infringement_id),
        )


async def _feed_fixture(
    migrated_db: str, confirm_store: PostgresConfirmStore, count: int
) -> list[UUID]:
    """``count`` triaged hits, each one minute older than the last — newest
    first is therefore the order they were created in, reversed."""
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    ids: list[UUID] = []
    for index in range(count):
        _user_ref, infringement_id = await _seeded_infringement(
            migrated_db, confirm_store, severity="benign_copy"
        )
        _set_first_seen(migrated_db, infringement_id, base - timedelta(minutes=index))
        ids.append(infringement_id)
    return ids


async def test_list_hits_pages_stably_across_a_boundary(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Five hits, two pages of two then a third: every hit appears exactly
    once, in order, and the page boundary neither repeats nor swallows one.

    The property that matters is the cursor's, not the LIMIT's — an offset
    pager would also pass a static fixture, and would still lose a row the
    moment a new hit arrived mid-page."""
    confirm_store, review_store = stores
    ids = await _feed_fixture(migrated_db, confirm_store, 5)

    seen: list[UUID] = []
    after: tuple[datetime, UUID] | None = None
    for _page in range(3):
        page = await review_store.list_hits(limit=2, after=after)
        seen.extend(hit["infringement_id"] for hit in page.hits)
        if not page.has_more:
            break
        last = page.hits[-1]
        after = (last["first_seen_at"], last["infringement_id"])

    assert seen == ids
    assert len(seen) == len(set(seen))


async def test_list_hits_carries_the_whole_reviewer_row(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """One triaged hit, every field a reviewer needs — including the two that
    come through the representative-attestation → run → seed chain, which is
    the part of the query most likely to be silently dropped by a refactor."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="explicit_unmatched"
    )

    page = await review_store.list_hits(limit=10)

    (hit,) = page.hits
    assert hit["infringement_id"] == infringement_id
    assert hit["user_ref"] == user_ref
    assert hit["source_domain"] == "example.test"
    assert hit["confirm_state"] == "machine_triaged"
    assert hit["severity"] == "explicit_unmatched"
    assert hit["face_match_score"] == 91.25
    # Label NAMES, not the stored {name, confidence} objects.
    assert hit["moderation_labels"] == ["Explicit Nudity"]
    assert hit["duplicate_of"] is None
    # image_url is set and triage carries a bbox object, so 0031's expression
    # is true — the operator preview can actually render this one.
    assert hit["preview_available"] is True
    assert hit["review_task"] is not None
    assert hit["review_task"]["status"] == "pending"
    assert hit["review_task"]["severity"] == "explicit_unmatched"
    # Nobody has decided or labelled it yet.
    assert hit["subject_decision"] is None
    assert hit["latest_verdict"] is None
    # The seed chain: which photo was searched, and in what form.
    assert hit["source_object_ref"] is not None
    assert hit["seed_kind"] == "user_supplied"


async def test_list_hits_never_shows_a_quarantined_hit_whatever_the_filters(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """A quarantined hit is CSAM-suspected. It is excluded from every svc
    view and from the subject's surface, and it is excluded here too — by the
    store's WHERE, so no filter can reach it. The filters are asked to reach
    it explicitly: by its own severity, and by `confirm_state='quarantined'`,
    which is the one value the route's Literal does not even accept."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await confirm_store.record_quarantine(
        infringement_id,
        phash=11,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 99.0}],
        min_age_low=8.0,
    )

    unfiltered = await review_store.list_hits(limit=50)
    by_severity = await review_store.list_hits(limit=50, severity="ncii_suspected")
    by_user = await review_store.list_hits(limit=50, user_ref=user_ref)
    by_state = await review_store.list_hits(limit=50, confirm_state="quarantined")

    for page in (unfiltered, by_severity, by_user, by_state):
        assert [hit["infringement_id"] for hit in page.hits] == []


async def test_a_duplicate_hit_IS_in_the_feed_and_is_filterable(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """ONLY `quarantined` is excluded. A `duplicate` hit appears.

    This is a cross-repo contract point, which is why it has its own test
    rather than riding on the quarantine one. A duplicate is an ordinary hit
    the confirm pipeline collapsed onto an earlier identical picture -- it is
    real, it is not CSAM-suspected, and it is exactly the kind of row a
    reviewer measuring the matcher wants to see. So the feed's
    `confirm_state` filter has FIVE accepted values, not four, and a caller
    that offers only four silently hides a whole class of hit.

    The exclusion that IS unconditional is asserted separately, and it is
    written in the SQL (`confirm_state <> 'quarantined'`) rather than merely
    never produced by accident.
    """
    confirm_store, review_store = stores
    _original_user, original = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    _dup_user, duplicate = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    await confirm_store.record_duplicate(duplicate, duplicate_of=original, phash=42)

    unfiltered = await review_store.list_hits(limit=50)
    filtered = await review_store.list_hits(limit=50, confirm_state="duplicate")

    assert duplicate in [hit["infringement_id"] for hit in unfiltered.hits]
    (only,) = filtered.hits
    assert only["infringement_id"] == duplicate
    assert only["confirm_state"] == "duplicate"
    # It carries its source, so a reviewer can see what it collapsed onto.
    assert only["duplicate_of"] == original


async def test_list_hits_filters_compose(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Two people, two severities. Each filter narrows, and together they
    narrow further — not "the last one wins", which is what a WHERE built by
    string concatenation tends to produce."""
    confirm_store, review_store = stores
    wanted_user, wanted = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    _other_user, _other = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )

    by_user = await review_store.list_hits(limit=50, user_ref=wanted_user)
    by_severity = await review_store.list_hits(limit=50, severity="benign_copy")
    both = await review_store.list_hits(
        limit=50, user_ref=wanted_user, severity="benign_copy"
    )
    contradictory = await review_store.list_hits(
        limit=50, user_ref=wanted_user, severity="unassessed"
    )

    assert [hit["infringement_id"] for hit in by_user.hits] == [wanted]
    assert [hit["infringement_id"] for hit in by_severity.hits] == [wanted]
    assert [hit["infringement_id"] for hit in both.hits] == [wanted]
    # Composed, not overridden. This user has no `unassessed` hit, and the
    # OTHER user does -- so a WHERE where the last filter wins would answer
    # with theirs.
    assert list(contradictory.hits) == []


async def test_list_hits_since_excludes_older_hits(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    ids = await _feed_fixture(migrated_db, confirm_store, 3)
    # ids[0] is the newest (12:00), ids[1] 11:59, ids[2] 11:58.
    cutoff = datetime(2026, 9, 14, 11, 58, 30, tzinfo=UTC)

    page = await review_store.list_hits(limit=50, since=cutoff)

    assert [hit["infringement_id"] for hit in page.hits] == ids[:2]


async def test_a_subject_decided_hit_carries_its_subject_decision(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """`subject_decision` is present iff the SUBJECT answered. A reviewer
    measuring the machine has to be able to tell whose answer they are looking
    at — an operator's decision and a machine confirm must not read as one."""
    confirm_store, review_store = stores
    user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )
    await review_store.subject_decide(
        infringement_id, user_ref=user_ref, decision="rejected"
    )
    _auto_user, auto_id = await _auto_confirmed_infringement(migrated_db, confirm_store)

    page = await review_store.list_hits(limit=50)
    by_id = {hit["infringement_id"]: hit for hit in page.hits}

    subject_hit = by_id[infringement_id]
    assert subject_hit["confirm_decided_by"] == "subject"
    assert subject_hit["subject_decision"] == {
        "decision": "rejected",
        "decided_at": subject_hit["confirm_decided_at"],
    }
    # The machine confirm is visible as a hit and carries NO subject decision.
    assert by_id[auto_id]["confirm_decided_by"] == AUTO_CONFIRM_DECIDED_BY
    assert by_id[auto_id]["subject_decision"] is None


# ── record_verdict: a label, never a decision ────────────────────────────


async def test_a_verdict_moves_no_state_and_writes_one_audit_row(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Owner decision D6, asserted rather than described: the `infringements`
    row and the `review_tasks` row are byte-identical either side of a
    verdict. Whole rows, not a field list — a field list only catches the
    columns somebody thought to name."""
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="explicit_unmatched"
    )
    before_hit = _row(
        migrated_db,
        "SELECT * FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    before_task = _row(
        migrated_db,
        "SELECT * FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )

    record = await review_store.record_verdict(
        infringement_id, operator="alice", verdict="false_positive", note="wrong face"
    )

    assert record is not None
    assert record.verdict == "false_positive"
    assert record.operator == "alice"
    assert record.note == "wrong face"
    # The snapshot: the hit AS IT STOOD, so a later severity override cannot
    # rewrite what this measurement was about.
    assert record.machine_severity == "explicit_unmatched"
    assert record.face_match_score == 91.25
    assert record.confirm_state_at_verdict == "machine_triaged"

    after_hit = _row(
        migrated_db,
        "SELECT * FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    after_task = _row(
        migrated_db,
        "SELECT * FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert after_hit == before_hit
    assert after_task == before_task

    audit = _rows(
        migrated_db,
        "SELECT action, actor_type, metadata FROM audit_log WHERE resource_id = %s",
        (infringement_id,),
    )
    assert len(audit) == 1
    assert audit[0]["action"] == REVIEW_VERDICT_ACTION
    assert audit[0]["actor_type"] == "operator"
    assert audit[0]["metadata"]["operator"] == "alice"
    assert audit[0]["metadata"]["verdict"] == "false_positive"
    assert audit[0]["metadata"]["machine_severity"] == "explicit_unmatched"


async def test_two_verdicts_both_survive_and_the_feed_shows_the_latest(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Append-only in behaviour, not only in grant: a second look writes a
    second row, and the feed reads the newer one."""
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )

    await review_store.record_verdict(
        infringement_id, operator="alice", verdict="false_positive", note=None
    )
    second = await review_store.record_verdict(
        infringement_id, operator="bob", verdict="true_positive", note="on reflection"
    )

    rows = _rows(
        migrated_db,
        "SELECT verdict FROM review_verdicts WHERE infringement_id = %s"
        " ORDER BY created_at",
        (infringement_id,),
    )
    assert [row["verdict"] for row in rows] == ["false_positive", "true_positive"]

    page = await review_store.list_hits(limit=10)
    (hit,) = page.hits
    assert second is not None
    assert hit["latest_verdict"]["verdict_id"] == second.verdict_id
    assert hit["latest_verdict"]["verdict"] == "true_positive"
    assert hit["latest_verdict"]["operator"] == "bob"


async def test_record_verdict_is_none_for_an_absent_or_quarantined_hit(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Both answer None, which the route maps to the same 404. A quarantined
    hit is excluded from every surface; labelling one would be looking at it."""
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="ncii_suspected"
    )
    await confirm_store.record_quarantine(
        infringement_id,
        phash=11,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 99.0}],
        min_age_low=8.0,
    )

    absent = await review_store.record_verdict(
        uuid4(), operator="alice", verdict="unsure", note=None
    )
    quarantined = await review_store.record_verdict(
        infringement_id, operator="alice", verdict="unsure", note=None
    )

    assert absent is None
    assert quarantined is None
    assert (
        _rows(migrated_db, "SELECT verdict_id FROM review_verdicts WHERE true", ()) == []
    )


# ── verdict_stats ────────────────────────────────────────────────────────

_WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)


async def test_verdict_stats_counts_the_latest_verdict_per_hit(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Two hits, three verdicts — one reviewer changed their mind. The rate
    must describe two hits, not three labels, or a reviewer who looked twice
    would move the machine's measured accuracy."""
    confirm_store, review_store = stores
    _u1, first = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    _u2, second = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    await review_store.record_verdict(
        first, operator="alice", verdict="false_positive", note=None
    )
    # The same hit, looked at again: only this one counts.
    await review_store.record_verdict(
        first, operator="alice", verdict="true_positive", note=None
    )
    await review_store.record_verdict(
        second, operator="bob", verdict="false_positive", note=None
    )

    stats = await review_store.verdict_stats(since=_WINDOW_START)

    (band,) = stats["by_severity"]
    assert band["machine_severity"] == "benign_copy"
    assert band["total"] == 2
    assert band["true_positive"] == 1
    assert band["false_positive"] == 1
    assert band["false_positive_rate"] == 0.5
    # by_operator is EVERY row: alice did two looks, bob one.
    assert stats["by_operator"] == [
        {
            "operator": "alice",
            "total": 2,
            "true_positive": 1,
            "false_positive": 1,
            "unsure": 0,
        },
        {
            "operator": "bob",
            "total": 1,
            "true_positive": 0,
            "false_positive": 1,
            "unsure": 0,
        },
    ]


async def test_verdict_stats_rate_is_none_when_nothing_was_decided(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """`unsure` counts in the total and in NEITHER side of the rate, so a
    window of nothing but `unsure` has a zero denominator. It must answer
    None: "we measured no false positives" and "we measured nothing" are
    different claims, and only one is safe in front of a decision about
    replacing the matcher."""
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )
    await review_store.record_verdict(
        infringement_id, operator="alice", verdict="unsure", note=None
    )

    stats = await review_store.verdict_stats(since=_WINDOW_START)

    (band,) = stats["by_severity"]
    assert band["total"] == 1
    assert band["unsure"] == 1
    assert band["false_positive_rate"] is None
    assert stats["subject_agreement"]["compared"] == 0
    assert stats["subject_agreement"]["agreement_rate"] is None


async def test_verdict_stats_measures_subject_agreement(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    """Four hits the subject decided and one an operator did.

    Agreement is confirmed-with-true_positive or rejected-with-false_positive.
    `unsure` is excluded from the comparison rather than scored as a
    disagreement — it is the absence of an opinion, and counting it would make
    a cautious reviewer look wrong. The operator-decided hit is excluded too:
    comparing a reviewer to a reviewer measures nothing.
    """
    confirm_store, review_store = stores
    agree_user, agreeing = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )
    disagree_user, disagreeing = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )
    unsure_user, unsure = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )
    _op_user, operator_decided = await _seeded_infringement(
        migrated_db, confirm_store, severity="unassessed"
    )

    await review_store.subject_decide(
        agreeing, user_ref=agree_user, decision="confirmed"
    )
    await review_store.record_verdict(
        agreeing, operator="alice", verdict="true_positive", note=None
    )
    await review_store.subject_decide(
        disagreeing, user_ref=disagree_user, decision="confirmed"
    )
    await review_store.record_verdict(
        disagreeing, operator="alice", verdict="false_positive", note=None
    )
    await review_store.subject_decide(unsure, user_ref=unsure_user, decision="rejected")
    await review_store.record_verdict(
        unsure, operator="alice", verdict="unsure", note=None
    )
    task_id = _row(
        migrated_db,
        "SELECT task_id FROM review_tasks WHERE infringement_id = %s",
        (operator_decided,),
    )["task_id"]
    await review_store.decide(
        task_id, decision="confirmed", operator="bob", severity=None
    )
    await review_store.record_verdict(
        operator_decided, operator="alice", verdict="true_positive", note=None
    )

    agreement = (await review_store.verdict_stats(since=_WINDOW_START))[
        "subject_agreement"
    ]

    assert agreement == {
        "compared": 2,
        "agreed": 1,
        "disagreed": 1,
        "agreement_rate": 0.5,
    }


async def test_verdict_stats_window_excludes_older_verdicts(
    migrated_db: str,
    stores: tuple[PostgresConfirmStore, PostgresReviewStore],
) -> None:
    confirm_store, review_store = stores
    _user_ref, infringement_id = await _seeded_infringement(
        migrated_db, confirm_store, severity="benign_copy"
    )
    await review_store.record_verdict(
        infringement_id, operator="alice", verdict="false_positive", note=None
    )

    stats = await review_store.verdict_stats(
        since=datetime.now(UTC) + timedelta(minutes=1)
    )

    assert stats["by_severity"] == []
    assert stats["by_operator"] == []
    assert stats["subject_agreement"]["compared"] == 0
