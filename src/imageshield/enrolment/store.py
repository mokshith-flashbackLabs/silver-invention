"""Persistence for ``enrolments`` — raw SQL, no ORM (CLAUDE.md §2).

:class:`EnrolmentStore` is the Protocol the deletion route depends on; the
Postgres implementation lands with the DELETE path. The enrolment INSERT
itself lives on the liveness store's ``finalize_enrolled`` — it must share a
transaction with the session consumption (migration 0003's composite FK).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from imageshield.enrolment.models import EnrolmentRow
from imageshield.types import UserRef


class EnrolmentStore(Protocol):
    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]: ...

    async def tombstone_enrolments(self, user_ref: UserRef) -> int: ...


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
