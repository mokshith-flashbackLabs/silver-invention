"""Persistence for ``enrolments`` — raw SQL, no ORM (CLAUDE.md §2).

:class:`EnrolmentStore` is the Protocol the deletion route depends on; the
Postgres implementation lands with the DELETE path. The enrolment INSERT
itself lives on the liveness store's ``finalize_enrolled`` — it must share a
transaction with the session consumption (migration 0003's composite FK).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from imageshield.enrolment.models import EnrolmentRow
from imageshield.types import UserRef

_ENROLMENT_COLUMNS = (
    "enrolment_id, session_id, user_ref, collection_id, external_face_id,"
    " quality_score, model_id, source_object_uri, status, created_at, deleted_at"
)

_ACTIVE_SQL = f"""
    SELECT {_ENROLMENT_COLUMNS} FROM enrolments
    WHERE user_ref = %(user_ref)s AND status = 'active'
    ORDER BY created_at
"""

# Soft delete (CLAUDE.md §5): never DELETE — biometric enrolments are
# expensive to recreate and the row is the only record the face ever existed.
_TOMBSTONE_SQL = """
    UPDATE enrolments
    SET status = 'deleted', deleted_at = now()
    WHERE user_ref = %(user_ref)s AND status = 'active'
"""


class EnrolmentStore(Protocol):
    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]: ...

    async def tombstone_enrolments(self, user_ref: UserRef) -> int: ...


class PostgresEnrolmentStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ACTIVE_SQL, {"user_ref": user_ref})
            records = await cur.fetchall()
        return tuple(to_enrolment_row(record) for record in records)

    async def tombstone_enrolments(self, user_ref: UserRef) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_TOMBSTONE_SQL, {"user_ref": user_ref})
            return cur.rowcount


def to_enrolment_row(record: tuple[Any, ...]) -> EnrolmentRow:
    (
        enrolment_id,
        session_id,
        user_ref,
        collection_id,
        external_face_id,
        quality_score,
        model_id,
        source_object_uri,
        status,
        created_at,
        deleted_at,
    ) = record
    return EnrolmentRow(
        enrolment_id=enrolment_id,
        session_id=session_id,
        user_ref=user_ref,
        collection_id=collection_id,
        external_face_id=external_face_id,
        quality_score=(
            float(quality_score) if isinstance(quality_score, Decimal) else quality_score
        ),
        model_id=model_id,
        source_object_uri=source_object_uri,
        status=str(status),
        created_at=created_at,
        deleted_at=deleted_at,
    )
