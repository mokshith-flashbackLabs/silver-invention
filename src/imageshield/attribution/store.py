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

from imageshield.attribution.models import (
    AttributedFace,
    AttributionOutcome,
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

# source_object_ref is the photo_ref — an opaque durable reference, never a URL
# (migration 0011). The presigned GET this run was given expires; the seed must
# not carry it.
_INSERT_SEED_SQL = """
    INSERT INTO search_seeds
      (user_ref, seed_kind, source_object_ref, scan_tier, attributed_face_id)
    VALUES
      (%(user_ref)s, 'user_supplied', %(source_object_ref)s, 'new',
       %(attributed_face_id)s)
    RETURNING seed_id
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
        seed_owners: tuple[tuple[UserRef, AttributedFace], ...],
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
        seed_owners: tuple[tuple[UserRef, AttributedFace], ...],
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
            seeds = await self._insert_seeds(conn, photo_ref, seed_owners, face_ids)
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
        photo_ref: str,
        seed_owners: tuple[tuple[UserRef, AttributedFace], ...],
        face_ids: dict[int, UUID],
    ) -> tuple[RegisteredSeed, ...]:
        seeds: list[RegisteredSeed] = []
        for user_ref, face in seed_owners:
            cur = await conn.execute(
                _INSERT_SEED_SQL,
                {
                    "user_ref": user_ref,
                    "source_object_ref": photo_ref,
                    "attributed_face_id": face_ids[face.face_index],
                },
            )
            row = await cur.fetchone()
            assert row is not None
            seeds.append(RegisteredSeed(user_ref=user_ref, seed_id=cast(UUID, row[0])))
        return tuple(seeds)
