"""The attribution store, against real Postgres.

The one-transaction guarantee is the point of this file: a half-written run —
some faces present, some missing — would be indistinguishable from "those faces
matched nobody", and those two must never be confusable, because the first is a
normal result and the second is a lost seed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest

from imageshield.attribution.models import AttributedFace, BoundingBox
from imageshield.attribution.store import PostgresAttributionStore
from imageshield.db.connection import make_async_pool
from imageshield.types import UserRef
from tests.db import ensure_subject, run_migrate

BOX = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresAttributionStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresAttributionStore(pool)
    finally:
        await pool.close()


def _face(index: int, ref: UserRef | None = None, score: float | None = None) -> AttributedFace:
    return AttributedFace(
        face_index=index,
        bbox=BOX,
        detect_confidence=99.5,
        resolved_user_ref=ref,
        match_score=score,
    )


async def _enrolled(store: PostgresAttributionStore, count: int = 1) -> list[UserRef]:
    refs = [UserRef(uuid4()) for _ in range(count)]
    for ref in refs:
        await ensure_subject(store._pool, ref)
    return refs


async def _record(
    store: PostgresAttributionStore,
    faces: tuple[AttributedFace, ...],
    seed_owners: tuple[tuple[UserRef, AttributedFace], ...],
    *,
    owner: UserRef,
    photo_ref: str = "photo-abc",
) -> object:
    return await store.record_run(
        photo_ref=photo_ref,
        requested_by=owner,
        candidate_count=2,
        match_threshold=92.0,
        max_candidates=5,
        model_id="rekognition:7.0",
        faces=faces,
        seed_owners=seed_owners,
    )


async def test_every_detected_face_is_stored_including_unattributed(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when: attributed_faces holds a bbox for EVERY detected face.

    Dropping the unattributed ones would make "we saw three faces and matched
    one" indistinguishable from "we saw one".
    """
    (owner,) = await _enrolled(store)
    matched = _face(1, owner, 95.0)
    faces = (_face(0), matched, _face(2))

    await _record(store, faces, ((owner, matched),), owner=owner)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT face_index, bbox, resolved_user_ref, match_score"
            " FROM attributed_faces ORDER BY face_index"
        ).fetchall()
    assert [row[0] for row in rows] == [0, 1, 2]
    assert all(row[1] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4} for row in rows)
    assert [row[2] for row in rows] == [None, owner, None]
    assert rows[0][3] is None and rows[2][3] is None


async def test_one_enrolled_face_and_two_strangers_registers_exactly_one_seed(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when. The face-level rule: a stranger in frame does not discard a
    valid seed for the owner."""
    (owner,) = await _enrolled(store)
    matched = _face(1, owner, 95.0)

    outcome = await _record(
        store, (_face(0), matched, _face(2)), ((owner, matched),), owner=owner
    )

    assert len(outcome.seeds) == 1  # type: ignore[attr-defined]
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        seeds = conn.execute(
            "SELECT user_ref, seed_kind, source_object_ref, attributed_face_id,"
            " scan_tier FROM search_seeds"
        ).fetchall()
    assert len(seeds) == 1
    assert seeds[0][0] == owner
    assert seeds[0][1] == "user_supplied"
    # The seed carries the opaque photo_ref, never the expiring presigned URL.
    assert seeds[0][2] == "photo-abc"
    assert seeds[0][3] is not None  # linked to the face that produced it
    assert seeds[0][4] == "new"


async def test_two_enrolled_household_members_register_two_seeds(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when: two seeds, one each, independent."""
    alice, bob = await _enrolled(store, 2)
    face_a, face_b = _face(0, alice, 95.0), _face(1, bob, 96.0)

    await _record(store, (face_a, face_b), ((alice, face_a), (bob, face_b)), owner=alice)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        seeds = conn.execute(
            "SELECT user_ref FROM search_seeds ORDER BY user_ref"
        ).fetchall()
    assert {row[0] for row in seeds} == {alice, bob}
    assert len(seeds) == 2


async def test_a_photo_with_no_enrolled_faces_registers_zero_seeds(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when: zero seeds, and a COMPLETED run — not an error."""
    (owner,) = await _enrolled(store)

    await _record(store, (_face(0), _face(1)), (), owner=owner)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM search_seeds").fetchone() == (0,)
        run = conn.execute(
            "SELECT status, faces_detected, faces_attributed FROM attribution_runs"
        ).fetchone()
    assert run == ("completed", 2, 0)


async def test_a_photo_with_no_faces_completes_with_an_empty_run(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when. 'There is nobody in this photo' is a fine answer."""
    (owner,) = await _enrolled(store)

    outcome = await _record(store, (), (), owner=owner)

    assert outcome.faces == ()  # type: ignore[attr-defined]
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        run = conn.execute(
            "SELECT status, faces_detected, faces_attributed FROM attribution_runs"
        ).fetchone()
    assert run == ("completed", 0, 0)


async def test_the_run_records_the_threshold_and_model_it_used(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """A later retune otherwise makes every historical attribution
    uninterpretable — the same reason search_runs.threshold_config exists."""
    (owner,) = await _enrolled(store)

    await _record(store, (), (), owner=owner)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT match_threshold, max_candidates, model_id, candidate_count"
            " FROM attribution_runs"
        ).fetchone()
    assert row is not None
    assert float(row[0]) == 92.0
    assert row[1] == 5
    assert row[2] == "rekognition:7.0"
    assert row[3] == 2


async def test_run_faces_and_seeds_commit_together_or_not_at_all(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """Done-when: kill mid-write, confirm none exist.

    Killing a process mid-transaction is not reproducible in-process, so this
    proves the equivalent at the level Postgres enforces it: a forced abort
    inside the transaction leaves NOTHING — no run, no faces, no seeds. The
    abort is a seed for an unparented user_ref, which migration 0008's
    search_seeds_subject_fk refuses AFTER the run and faces have been inserted.
    """
    (owner,) = await _enrolled(store)
    unenrolled = UserRef(uuid4())  # no subjects row -> the seed insert will fail
    matched = _face(0, unenrolled, 95.0)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await _record(store, (matched,), ((unenrolled, matched),), owner=owner)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM attribution_runs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM attributed_faces").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM search_seeds").fetchone() == (0,)


async def test_a_failed_run_is_recorded_and_stays_distinguishable(
    store: PostgresAttributionStore, migrated_db: str
) -> None:
    """'We tried and could not' is a different fact from 'we looked and matched
    nobody'. Only one of them is worth retrying."""
    (owner,) = await _enrolled(store)

    await store.record_failed_run(
        photo_ref="photo-abc",
        requested_by=owner,
        candidate_count=1,
        match_threshold=92.0,
        max_candidates=5,
        model_id="rekognition:7.0",
        error_detail="DetectFaces failed with ThrottlingException",
    )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, error_detail, faces_detected FROM attribution_runs"
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert "Throttling" in row[1]
    assert row[2] is None  # never claims to have detected zero faces
