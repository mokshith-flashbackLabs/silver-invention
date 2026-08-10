"""PostgresEnrolmentStore against real Postgres (same convention as
tests/test_liveness_store.py: own down --all + up arrange step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from imageshield.db.connection import make_async_pool
from imageshield.enrolment.models import NewEnrolment
from imageshield.enrolment.store import PostgresEnrolmentStore
from imageshield.liveness.models import LivenessSessionRow
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
