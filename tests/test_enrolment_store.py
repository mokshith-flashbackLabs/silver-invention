"""PostgresEnrolmentStore against real Postgres (same convention as
tests/test_liveness_store.py: own down --all + up arrange step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from imageshield.db.connection import make_async_pool
from imageshield.enrolment.models import IDENTITY_CONFLICT_REASON, Collision, NewEnrolment
from imageshield.enrolment.store import PostgresEnrolmentStore
from imageshield.liveness.models import LivenessSessionRow
from imageshield.liveness.store import PostgresLivenessStore
from imageshield.subjects.eligibility import eligibility_for
from imageshield.types import SessionId, UserRef
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
) -> AsyncIterator[tuple[PostgresLivenessStore, PostgresEnrolmentStore]]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresLivenessStore(pool), PostgresEnrolmentStore(pool)
    finally:
        await pool.close()


async def _enrol_user(liveness: PostgresLivenessStore, user_ref: UserRef) -> str:
    """Create + pass + enrol one session; return the external_face_id."""
    row = await liveness.create_session(
        user_ref=user_ref,
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=600,
        max_attempts_24h=5,
    )
    assert isinstance(row, LivenessSessionRow), f"expected a row, got {row}"
    face_id = f"face-{uuid4()}"
    outcome = await liveness.finalize_enrolled(
        row.session_id,
        confidence=98.0,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=NewEnrolment(
            user_ref=user_ref,
            collection_id="identity-v1",
            external_face_id=face_id,
            quality_score=99.0,
            model_id="rekognition:7.0",
            source_object_uri="https://proxy-s3.example/ref.jpg",
            consent_ref=uuid4(),
            consent_document_sha256="sha256:" + "a" * 64,
            consent_signed_at=datetime.now(UTC),
        ),
        eligibility=eligibility_for(True),
    )
    assert outcome is not None
    return face_id


async def test_get_active_returns_only_this_users_active_rows(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, enrolments = stores
    user_ref, other = UserRef(uuid4()), UserRef(uuid4())
    face_id = await _enrol_user(liveness, user_ref)
    await _enrol_user(liveness, other)

    active = await enrolments.get_active_enrolments(user_ref)

    assert [e.external_face_id for e in active] == [face_id]
    assert all(e.status == "active" for e in active)


async def test_tombstone_flips_status_and_is_idempotent(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, enrolments = stores
    user_ref = UserRef(uuid4())
    await _enrol_user(liveness, user_ref)

    first = await enrolments.tombstone_enrolments(user_ref)
    second = await enrolments.tombstone_enrolments(user_ref)

    assert first == 1
    assert second == 0  # nothing active left: idempotent
    assert await enrolments.get_active_enrolments(user_ref) == ()  # type: ignore[arg-type]


# ── finalize_conflict (migration 0036, spec 2026-09-22) ────────────────────


async def _created_session(liveness: PostgresLivenessStore) -> LivenessSessionRow:
    row = await liveness.create_session(
        user_ref=UserRef(uuid4()),
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=600,
        max_attempts_24h=5,
    )
    assert isinstance(row, LivenessSessionRow), f"expected a row, got {row}"
    return row


def _collision(them: UserRef) -> Collision:
    return Collision(
        matched_user_ref=them, similarity=98.4, model_id="rekognition:7.0", threshold_used=97.0
    )


async def test_finalize_conflict_consumes_and_records_in_one_transaction(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, enrolments = stores
    row = await _created_session(liveness)
    them = UserRef(uuid4())

    outcome = await liveness.finalize_conflict(
        SessionId(row.session_id),
        confidence=99.1,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=("https://proxy-s3.example/a0.jpg",),
        collection_id="identity-v1",
        collision=_collision(them),
    )

    assert outcome is not None
    session, conflict = outcome
    assert session.status == "consumed"
    assert session.failure_reason == IDENTITY_CONFLICT_REASON
    assert session.consumed_at is not None and session.completed_at is not None
    assert conflict.session_id == row.session_id
    assert conflict.attempted_user_ref == row.user_ref
    assert conflict.matched_user_ref == them
    assert conflict.similarity == 98.4
    assert conflict.threshold_used == 97.0
    assert conflict.collection_id == "identity-v1"
    # A refused frame creates nobody: no enrolment, hence no consent ref.
    assert await liveness.get_enrolment_consent_ref(SessionId(row.session_id)) is None
    assert tuple(await enrolments.get_active_enrolments(UserRef(row.user_ref))) == ()
    assert await liveness.get_conflict(SessionId(row.session_id)) == conflict


async def test_finalize_conflict_loses_cleanly_to_a_concurrent_finalizer(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, _ = stores
    row = await _created_session(liveness)
    await liveness.finalize_quality_rejected(
        SessionId(row.session_id),
        confidence=99.0,
        reference_image_uri="https://x/ref.jpg",
        audit_image_uris=(),
    )

    outcome = await liveness.finalize_conflict(
        SessionId(row.session_id),
        confidence=99.0,
        reference_image_uri="https://x/ref.jpg",
        audit_image_uris=(),
        collection_id="identity-v1",
        collision=_collision(UserRef(uuid4())),
    )

    assert outcome is None
    assert await liveness.get_conflict(SessionId(row.session_id)) is None
