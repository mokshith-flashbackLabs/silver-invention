"""PostgresLivenessStore against real Postgres (Task-1 throwaway harness).

Same coordination convention as ``tests/test_outbox.py``: each test file that
needs the migrated schema runs its own ``down --all`` + ``up`` arrange step so
files don't fight over the shared session-scoped database.

The store owns every SQL statement the liveness routes need; these tests prove
the semantics against Postgres's own clock, type system (NUMERIC, TEXT[],
liveness_status enum) and migration 0002's ``result_idempotency_key`` column —
things the in-memory fake in ``tests/test_liveness_routes.py`` cannot prove.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.enrolment.models import (
    QUALITY_REJECTED_REASON,
    SENTINEL_CONSENT_REF,
    NewEnrolment,
)
from imageshield.liveness.models import CreateRejection, LivenessSessionRow
from imageshield.liveness.store import PostgresLivenessStore
from imageshield.subjects.eligibility import eligibility_for
from imageshield.types import UserRef
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresLivenessStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresLivenessStore(pool)
    finally:
        await pool.close()


async def _create(
    store: PostgresLivenessStore, user_ref: object = None, **kwargs: object
) -> LivenessSessionRow:
    row = await store.create_session(
        user_ref=user_ref or uuid4(),  # type: ignore[arg-type]
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=int(kwargs.get("ttl_seconds", 600)),  # type: ignore[arg-type]
        max_attempts_24h=int(kwargs.get("max_attempts_24h", 5)),  # type: ignore[arg-type]
    )
    assert isinstance(row, LivenessSessionRow), f"expected a row, got {row}"
    return row


async def _backdate_created_at(dsn: str, session_id: object, hours: int) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            "UPDATE liveness_sessions"
            " SET created_at = now() - make_interval(hours => %s)"
            " WHERE session_id = %s",
            (hours, session_id),
        )


async def test_create_session_returns_persisted_row(store: PostgresLivenessStore) -> None:
    user_ref = uuid4()
    row = await _create(store, user_ref)

    assert row.user_ref == user_ref
    assert row.status == "created"
    assert row.attempt_number == 1
    assert row.confidence is None
    assert row.consumed_at is None
    assert row.result_idempotency_key is None

    fetched = await store.get_session(row.session_id)
    assert fetched == row


async def test_create_session_ttl_sets_expires_at(store: PostgresLivenessStore) -> None:
    row = await _create(store, ttl_seconds=600)
    delta = (row.expires_at - row.created_at).total_seconds()
    assert 599 <= delta <= 601


async def test_attempt_number_increments_per_user(store: PostgresLivenessStore) -> None:
    user_ref = uuid4()
    first = await _create(store, user_ref)
    second = await _create(store, user_ref)
    other = await _create(store)  # another user starts back at 1

    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert other.attempt_number == 1


async def test_create_session_rejects_when_attempts_exhausted(
    store: PostgresLivenessStore,
) -> None:
    user_ref = uuid4()
    for _ in range(3):
        await _create(store, user_ref, max_attempts_24h=3)

    result = await store.create_session(
        user_ref=user_ref,
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=600,
        max_attempts_24h=3,
    )

    assert result is CreateRejection.ATTEMPTS_EXCEEDED


async def test_attempts_older_than_24h_do_not_count(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    user_ref = uuid4()
    for _ in range(3):
        row = await _create(store, user_ref, max_attempts_24h=3)
        await _backdate_created_at(migrated_db, row.session_id, hours=25)

    result = await _create(store, user_ref, max_attempts_24h=3)
    assert result.attempt_number == 4, "attempt_number counts all-time, the cap counts 24h"


async def test_create_session_rejects_when_passed_unconsumed_exists(
    store: PostgresLivenessStore,
) -> None:
    user_ref = uuid4()
    row = await _create(store, user_ref)
    await store.claim_result(row.session_id, "idem-1")
    await store.finalize_result(
        row.session_id,
        status="passed",
        confidence=99.5,
        failure_reason=None,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=("https://proxy-s3.example/a0.jpg",),
    )

    result = await store.create_session(
        user_ref=user_ref,
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=600,
        max_attempts_24h=5,
    )

    assert result is CreateRejection.PASSED_UNCONSUMED


async def test_check_create_allowed_mirrors_create_session(
    store: PostgresLivenessStore,
) -> None:
    user_ref = uuid4()
    assert await store.check_create_allowed(user_ref, max_attempts_24h=1) is None

    await _create(store, user_ref)

    assert (
        await store.check_create_allowed(user_ref, max_attempts_24h=1)
        is CreateRejection.ATTEMPTS_EXCEEDED
    )


async def test_finalize_result_persists_all_fields(store: PostgresLivenessStore) -> None:
    row = await _create(store)
    await store.claim_result(row.session_id, "idem-key-9")

    finalized = await store.finalize_result(
        row.session_id,
        status="passed",
        confidence=98.75,
        failure_reason=None,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(
            "https://proxy-s3.example/a0.jpg",
            "https://proxy-s3.example/a1.jpg",
        ),
    )

    assert finalized.status == "passed"
    assert finalized.confidence == 98.75  # NUMERIC(5,2) round-trips exactly
    assert finalized.completed_at is not None
    assert finalized.reference_image_uri == "https://proxy-s3.example/ref.jpg"
    assert finalized.audit_image_uris == (
        "https://proxy-s3.example/a0.jpg",
        "https://proxy-s3.example/a1.jpg",
    )
    # migration 0002: the idempotency key survives the round-trip.
    assert finalized.result_idempotency_key == "idem-key-9"


async def test_finalize_failed_records_reason_without_uris(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    finalized = await store.finalize_result(
        row.session_id,
        status="failed",
        confidence=42.0,
        failure_reason="confidence_below_threshold",
        reference_image_uri=None,
        audit_image_uris=None,
    )

    assert finalized.status == "failed"
    assert finalized.failure_reason == "confidence_below_threshold"
    assert finalized.reference_image_uri is None
    assert finalized.audit_image_uris is None


async def test_claim_result_does_not_overwrite_after_completion(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    await store.claim_result(row.session_id, "idem-first")
    await store.finalize_result(
        row.session_id,
        status="failed",
        confidence=10.0,
        failure_reason="provider_reported_failure",
        reference_image_uri=None,
        audit_image_uris=None,
    )

    await store.claim_result(row.session_id, "idem-second")

    fetched = await store.get_session(row.session_id)
    assert fetched is not None
    assert fetched.result_idempotency_key == "idem-first"


async def test_mark_expired_sets_status(store: PostgresLivenessStore) -> None:
    row = await _create(store)
    expired = await store.mark_expired(row.session_id)
    assert expired.status == "expired"


async def test_get_unknown_session_returns_none(store: PostgresLivenessStore) -> None:
    assert await store.get_session(uuid4()) is None


# --- Step 4: finalize_enrolled / finalize_quality_rejected -------------------


CONSENT_SIGNED_AT = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)


def _new_enrolment(user_ref: UserRef, **overrides: Any) -> NewEnrolment:
    defaults: dict[str, Any] = {
        "user_ref": user_ref,
        "collection_id": "identity-v1",
        "external_face_id": f"face-{uuid4()}",
        "quality_score": 99.5,
        "model_id": "rekognition:7.0",
        "source_object_uri": "https://proxy-s3.example/ref.jpg",
        "consent_ref": uuid4(),
        "consent_document_sha256": "sha256:" + "b" * 64,
        "consent_signed_at": CONSENT_SIGNED_AT,
    }
    defaults.update(overrides)
    return NewEnrolment(**defaults)


async def test_finalize_enrolled_consumes_and_inserts_atomically(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    await store.claim_result(row.session_id, "idem-1")

    outcome = await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=("https://proxy-s3.example/audit-0.jpg",),
        enrolment=_new_enrolment(row.user_ref),
        eligibility=eligibility_for(True),
    )

    assert outcome is not None
    session, enrolment = outcome
    assert session.status == "consumed"
    assert session.consumed_at is not None
    assert session.completed_at is not None
    assert session.failure_reason is None
    assert session.confidence == 98.7
    assert enrolment.session_id == row.session_id
    assert enrolment.user_ref == row.user_ref
    assert enrolment.status == "active"
    assert enrolment.model_id == "rekognition:7.0"


async def test_the_subject_row_and_the_enrolment_row_commit_together(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    """Step-8 done-when: the subject row and enrolment row commit in ONE
    transaction — kill between and confirm neither.

    Killing a process mid-transaction is not directly reproducible in-process,
    so this proves the equivalent guarantee at the level Postgres enforces it:
    a forced abort inside the transaction leaves NOTHING, including the session
    consumption that happens first. There is no interleaving in which an
    enrolment exists without its eligibility flag, or vice versa.
    """
    row = await _create(store)
    duplicate_face = _new_enrolment(row.user_ref)

    # Second session, same external_face_id. enrolments_face_uniq aborts the
    # transaction AFTER the session UPDATE and the subjects upsert have run.
    await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=duplicate_face,
        eligibility=eligibility_for(True),
    )
    second = await _create(store)

    with pytest.raises(psycopg.errors.UniqueViolation):
        await store.finalize_enrolled(
            second.session_id,
            confidence=98.7,
            reference_image_uri="https://proxy-s3.example/ref2.jpg",
            audit_image_uris=(),
            enrolment=duplicate_face,
            eligibility=eligibility_for(False),  # would flip the flag, if it landed
        )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        # The second session is untouched...
        assert conn.execute(
            "SELECT status FROM liveness_sessions WHERE session_id = %s",
            (second.session_id,),
        ).fetchone() == ("created",)
        # ...and the first subject's flag was NOT overwritten by the aborted
        # transaction's ineligible assertion.
        assert conn.execute(
            "SELECT discovery_eligible, eligibility_reason FROM subjects"
            " WHERE user_ref = %s",
            (row.user_ref,),
        ).fetchone() == (True, "adult")
        assert conn.execute("SELECT count(*) FROM enrolments").fetchone() == (1,)


async def test_finalize_enrolled_writes_the_subject_row_in_the_same_call(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)

    await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=_new_enrolment(row.user_ref),
        eligibility=eligibility_for(False),
    )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(
            "SELECT discovery_eligible, eligibility_reason FROM subjects"
            " WHERE user_ref = %s",
            (row.user_ref,),
        ).fetchone() == (False, "minor_discovery_deferred")


async def test_quality_rejection_writes_no_subject_row(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    """Only the enrolling path asserts eligibility. No enrolment means no
    subject, which means no seed and no discovery — the safe direction."""
    row = await _create(store)

    await store.finalize_quality_rejected(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
    )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM subjects WHERE user_ref = %s", (row.user_ref,)
        ).fetchone() == (0,)


async def test_finalize_enrolled_returns_none_when_already_finalized(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    await store.finalize_result(
        row.session_id,
        status="failed",
        confidence=10.0,
        failure_reason="provider_reported_failure",
        reference_image_uri=None,
        audit_image_uris=None,
    )

    outcome = await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=_new_enrolment(row.user_ref),
        eligibility=eligibility_for(True),
    )

    assert outcome is None  # and, per the FK, no enrolment row can exist
    refetched = await store.get_session(row.session_id)
    assert refetched is not None and refetched.status == "failed"


async def test_finalize_quality_rejected_consumes_without_enrolment(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)

    session = await store.finalize_quality_rejected(
        row.session_id,
        confidence=97.0,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
    )

    assert session is not None
    assert session.status == "consumed"
    assert session.consumed_at is not None
    assert session.failure_reason == QUALITY_REJECTED_REASON
    async with await psycopg.AsyncConnection.connect(migrated_db) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM enrolments WHERE session_id = %s", (row.session_id,)
        )
        record = await cur.fetchone()
        assert record is not None and record[0] == 0


async def test_finalize_enrolled_emits_notify_in_the_transaction(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)
    async with await psycopg.AsyncConnection.connect(
        migrated_db, autocommit=True
    ) as listener:
        await listener.execute("LISTEN enrolment_complete")

        await store.finalize_enrolled(
            row.session_id,
            confidence=98.7,
            reference_image_uri="https://proxy-s3.example/ref.jpg",
            audit_image_uris=(),
            enrolment=_new_enrolment(row.user_ref),
            eligibility=eligibility_for(True),
        )

        gen = listener.notifies()
        notification = await asyncio.wait_for(anext(gen), timeout=5)
        await gen.aclose()
    assert notification.payload == str(row.session_id)


# --- consent_ref: recorded here, owned by the proxy -------------------------


async def test_finalize_enrolled_persists_the_consent_evidence(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)
    consent_ref = uuid4()

    outcome = await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=_new_enrolment(row.user_ref, consent_ref=consent_ref),
        eligibility=eligibility_for(True),
    )

    assert outcome is not None
    _, enrolment = outcome
    assert enrolment.consent_ref == consent_ref
    assert enrolment.consent_signed_at == CONSENT_SIGNED_AT
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        stored = conn.execute(
            "SELECT consent_ref, consent_document_sha256, consent_signed_at"
            " FROM enrolments WHERE session_id = %s",
            (row.session_id,),
        ).fetchone()
    assert stored == (consent_ref, "sha256:" + "b" * 64, CONSENT_SIGNED_AT)


async def test_consent_the_enrolment_and_the_consume_commit_together(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    """Done-when: all three consent fields land in the SAME transaction as the
    index and the session consume — kill mid-write and confirm no partial row.

    Killing a process mid-transaction is not reproducible in-process, so this
    proves the equivalent at the level Postgres enforces it: a forced abort
    inside the transaction leaves NOTHING behind, including the session
    consumption that runs first. There is no interleaving that yields an
    enrolment without the consent that authorised it, and none that consumes a
    session for an enrolment that never landed.
    """
    first = await _create(store)
    duplicate_face = _new_enrolment(first.user_ref)
    await store.finalize_enrolled(
        first.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=duplicate_face,
        eligibility=eligibility_for(True),
    )

    # Second session, same external_face_id: enrolments_face_uniq aborts the
    # transaction after the consume has already run.
    second = await _create(store)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await store.finalize_enrolled(
            second.session_id,
            confidence=98.7,
            reference_image_uri="https://proxy-s3.example/ref2.jpg",
            audit_image_uris=(),
            enrolment=duplicate_face,
            eligibility=eligibility_for(True),
        )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM liveness_sessions WHERE session_id = %s",
            (second.session_id,),
        ).fetchone() == ("created",)
        assert conn.execute("SELECT count(*) FROM enrolments").fetchone() == (1,)


async def test_the_database_refuses_the_sentinel_consent_ref(
    store: PostgresLivenessStore,
) -> None:
    """The route rejects it with a 422 first, but the constraint is what makes
    it impossible: an app-layer check alone does not survive a new writer."""
    row = await _create(store)

    with pytest.raises(psycopg.errors.CheckViolation):
        await store.finalize_enrolled(
            row.session_id,
            confidence=98.7,
            reference_image_uri="https://proxy-s3.example/ref.jpg",
            audit_image_uris=(),
            enrolment=_new_enrolment(row.user_ref, consent_ref=SENTINEL_CONSENT_REF),
            eligibility=eligibility_for(True),
        )


async def test_get_enrolment_consent_ref_reads_back_what_was_written(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    consent_ref = uuid4()
    await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=_new_enrolment(row.user_ref, consent_ref=consent_ref),
        eligibility=eligibility_for(True),
    )

    assert await store.get_enrolment_consent_ref(row.session_id) == consent_ref


async def test_get_enrolment_consent_ref_is_none_when_nothing_enrolled(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    assert await store.get_enrolment_consent_ref(row.session_id) is None
