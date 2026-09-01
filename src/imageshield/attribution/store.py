"""Persistence for attribution — raw SQL, no ORM (CLAUDE.md §2).

The run, its faces, and the seeds they produced commit in ONE transaction.
Killing the process mid-write leaves none of them. The alternative — a run row
written first, faces appended as they resolve — produces a half-attributed
photo that looks complete: some faces present, some missing, and no way from
the data to tell "this face matched nobody" from "we crashed before asking".
Those two must never be confusable, because the first is a normal result and
the second is a lost seed.

Ordering inside the transaction is fixed by the FKs: run, then faces, then
seeds (``search_seeds.attributed_face_id`` points at a face, which points at a
run).
"""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.attribution.crop_upload import SkippedSeed
from imageshield.attribution.models import (
    AttributedFace,
    AttributionOutcome,
    PlannedSeed,
    RegisteredSeed,
)
from imageshield.types import UserRef

_INSERT_RUN_SQL = """
    INSERT INTO attribution_runs
      (photo_ref, requested_by, candidate_count, match_threshold, max_candidates,
       model_id, faces_detected, faces_attributed, status, completed_at)
    VALUES
      (%(photo_ref)s, %(requested_by)s, %(candidate_count)s, %(match_threshold)s,
       %(max_candidates)s, %(model_id)s, %(faces_detected)s, %(faces_attributed)s,
       'completed', now())
    RETURNING run_id
"""

# A run that never got past the provider. Recorded rather than dropped: "we
# tried and could not" is a different fact from "we looked and nobody matched",
# and only one of them is worth retrying.
_INSERT_FAILED_RUN_SQL = """
    INSERT INTO attribution_runs
      (photo_ref, requested_by, candidate_count, match_threshold, max_candidates,
       model_id, status, error_detail, completed_at)
    VALUES
      (%(photo_ref)s, %(requested_by)s, %(candidate_count)s, %(match_threshold)s,
       %(max_candidates)s, %(model_id)s, 'failed', %(error_detail)s, now())
    RETURNING run_id
"""

# EVERY detected face, attributed or not. The bbox is provenance for a decision
# and the proxy renders boxes from it; dropping the unattributed ones would
# make "we saw three faces and matched one" indistinguishable from "we saw one".
_INSERT_FACE_SQL = """
    INSERT INTO attributed_faces
      (run_id, face_index, bbox, detect_confidence, resolved_user_ref,
       match_score, model_id)
    VALUES
      (%(run_id)s, %(face_index)s, %(bbox)s, %(detect_confidence)s,
       %(resolved_user_ref)s, %(match_score)s, %(model_id)s)
    RETURNING face_id
"""

# source_object_ref is an opaque durable reference, never a URL (migration
# 0011). The presigned GET this run was given expires; the seed must not carry
# it.
#
# Both the ref and the kind are now PARAMETERS rather than 'user_supplied' and
# the photo: on a group photo each subject's seed is a crop of their own face
# with kind 'face_crop' (spec 2026-08-31). The caller decides which; this just
# writes what it is handed.
_INSERT_SEED_SQL = """
    INSERT INTO search_seeds
      (user_ref, seed_kind, source_object_ref, scan_tier, attributed_face_id)
    VALUES
      (%(user_ref)s, %(seed_kind)s, %(source_object_ref)s, 'new',
       %(attributed_face_id)s)
    RETURNING seed_id
"""

# A subject we meant to seed and did not, because their crop never reached the
# proxy's bucket. In the SAME transaction as the run, so "the run completed" and
# "one of its subjects has no seed" can never disagree. Without this the only
# evidence is a warning log, and a missing seed looks exactly like a person who
# was not in the photo.
_INSERT_SKIPPED_SEED_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('service', 'attribution.seed_skipped', %(subject_ref)s, %(run_id)s,
            %(metadata)s)
"""


class AttributionStore(Protocol):
    async def record_run(
        self,
        *,
        photo_ref: str,
        requested_by: UserRef,
        candidate_count: int,
        match_threshold: float,
        max_candidates: int,
        model_id: str,
        faces: tuple[AttributedFace, ...],
        planned_seeds: tuple[PlannedSeed, ...],
        skipped_seeds: tuple[SkippedSeed, ...] = (),
    ) -> AttributionOutcome: ...

    async def record_failed_run(
        self,
        *,
        photo_ref: str,
        requested_by: UserRef,
        candidate_count: int,
        match_threshold: float,
        max_candidates: int,
        model_id: str,
        error_detail: str,
    ) -> UUID: ...


class PostgresAttributionStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record_run(
        self,
        *,
        photo_ref: str,
        requested_by: UserRef,
        candidate_count: int,
        match_threshold: float,
        max_candidates: int,
        model_id: str,
        faces: tuple[AttributedFace, ...],
        planned_seeds: tuple[PlannedSeed, ...],
        skipped_seeds: tuple[SkippedSeed, ...] = (),
    ) -> AttributionOutcome:
        attributed = sum(1 for face in faces if face.resolved_user_ref is not None)
        async with self._pool.connection() as conn, conn.transaction():
            run_id = await self._insert_run(
                conn,
                photo_ref=photo_ref,
                requested_by=requested_by,
                candidate_count=candidate_count,
                match_threshold=match_threshold,
                max_candidates=max_candidates,
                model_id=model_id,
                faces_detected=len(faces),
                faces_attributed=attributed,
            )
            face_ids = await self._insert_faces(conn, run_id, faces, model_id)
            seeds = await self._insert_seeds(conn, planned_seeds, face_ids)
            await self._insert_skipped_audit(conn, run_id, photo_ref, skipped_seeds)
        return AttributionOutcome(run_id=run_id, faces=faces, seeds=seeds)

    async def record_failed_run(
        self,
        *,
        photo_ref: str,
        requested_by: UserRef,
        candidate_count: int,
        match_threshold: float,
        max_candidates: int,
        model_id: str,
        error_detail: str,
    ) -> UUID:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _INSERT_FAILED_RUN_SQL,
                {
                    "photo_ref": photo_ref,
                    "requested_by": requested_by,
                    "candidate_count": candidate_count,
                    "match_threshold": match_threshold,
                    "max_candidates": max_candidates,
                    "model_id": model_id,
                    "error_detail": error_detail,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        run_id: UUID = row[0]
        return run_id

    @staticmethod
    async def _insert_run(
        conn: AsyncConnection[tuple[object, ...]],
        *,
        photo_ref: str,
        requested_by: UserRef,
        candidate_count: int,
        match_threshold: float,
        max_candidates: int,
        model_id: str,
        faces_detected: int,
        faces_attributed: int,
    ) -> UUID:
        cur = await conn.execute(
            _INSERT_RUN_SQL,
            {
                "photo_ref": photo_ref,
                "requested_by": requested_by,
                "candidate_count": candidate_count,
                "match_threshold": match_threshold,
                "max_candidates": max_candidates,
                "model_id": model_id,
                "faces_detected": faces_detected,
                "faces_attributed": faces_attributed,
            },
        )
        row = await cur.fetchone()
        assert row is not None
        return cast(UUID, row[0])

    @staticmethod
    async def _insert_faces(
        conn: AsyncConnection[tuple[object, ...]],
        run_id: UUID,
        faces: tuple[AttributedFace, ...],
        model_id: str,
    ) -> dict[int, UUID]:
        face_ids: dict[int, UUID] = {}
        for face in faces:
            cur = await conn.execute(
                _INSERT_FACE_SQL,
                {
                    "run_id": run_id,
                    "face_index": face.face_index,
                    "bbox": Jsonb(face.bbox.as_dict()),
                    "detect_confidence": face.detect_confidence,
                    "resolved_user_ref": face.resolved_user_ref,
                    "match_score": face.match_score,
                    "model_id": model_id,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            face_ids[face.face_index] = cast(UUID, row[0])
        return face_ids

    @staticmethod
    async def _insert_seeds(
        conn: AsyncConnection[tuple[object, ...]],
        planned_seeds: tuple[PlannedSeed, ...],
        face_ids: dict[int, UUID],
    ) -> tuple[RegisteredSeed, ...]:
        seeds: list[RegisteredSeed] = []
        for plan in planned_seeds:
            cur = await conn.execute(
                _INSERT_SEED_SQL,
                {
                    "user_ref": plan.user_ref,
                    "seed_kind": plan.seed_kind,
                    "source_object_ref": plan.source_object_ref,
                    "attributed_face_id": face_ids[plan.face.face_index],
                },
            )
            row = await cur.fetchone()
            assert row is not None
            seeds.append(
                RegisteredSeed(
                    user_ref=plan.user_ref,
                    seed_id=cast(UUID, row[0]),
                    crop_object_ref=plan.crop_object_ref,
                )
            )
        return tuple(seeds)

    @staticmethod
    async def _insert_skipped_audit(
        conn: AsyncConnection[tuple[object, ...]],
        run_id: UUID,
        photo_ref: str,
        skipped_seeds: tuple[SkippedSeed, ...],
    ) -> None:
        for skipped in skipped_seeds:
            await conn.execute(
                _INSERT_SKIPPED_SEED_AUDIT_SQL,
                {
                    "subject_ref": skipped.user_ref,
                    "run_id": run_id,
                    # photo_ref is an opaque object key, never a URL — safe to
                    # record, and it is the only way to find the photo this
                    # subject is now unmonitored for.
                    "metadata": Jsonb(
                        {"reason": skipped.reason, "photo_ref": photo_ref}
                    ),
                },
            )
