"""Enrolment deletion (CLAUDE.md §8 step 4, INVARIANTS #7).

Order is the invariant: DeleteFaces, VERIFY absence via ListFaces, and only
then tombstone. A crash mid-way leaves an active row pointing at deleted
faces — the retry completes it. It never leaves a searchable face with no
record pointing at it, which is unrecoverable without a full collection audit.

Nothing calls this in v1. It exists because the old system called DeleteFaces
nowhere (under a comment asserting BIPA compliance), so every face ever
enrolled there is still searchable, including deleted accounts'.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Response

from imageshield.enrolment.faceindex import FaceIndex
from imageshield.enrolment.models import FaceIndexUnavailable
from imageshield.enrolment.store import EnrolmentStore
from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_enrolment_store, get_face_index
from imageshield.http.errors import ServiceError
from imageshield.types import UserRef

log = structlog.get_logger("imageshield.enrolment")

router = APIRouter(prefix="/v1/enrolments", dependencies=[Depends(require_service_token)])


@router.delete("/{user_ref}", status_code=204)
async def delete_enrolments(
    user_ref: UUID,
    store: EnrolmentStore = Depends(get_enrolment_store),
    face_index: FaceIndex = Depends(get_face_index),
) -> Response:
    ref = UserRef(user_ref)
    active = await store.get_active_enrolments(ref)
    if not active:
        # Idempotent: nothing active means nothing searchable — the goal state.
        return Response(status_code=204)

    by_collection: dict[str, list[str]] = {}
    for enrolment in active:
        by_collection.setdefault(enrolment.collection_id, []).append(
            enrolment.external_face_id
        )

    try:
        for collection_id, face_ids in by_collection.items():
            await face_index.delete_faces(collection_id, tuple(face_ids))
            remaining = await face_index.list_face_ids(collection_id, tuple(face_ids))
            if remaining:
                # ABORT before the tombstone: a tombstoned row with a live
                # face is the unrecoverable state (INVARIANTS #7).
                log.error(
                    "enrolment.delete_unverified",
                    user_ref=str(ref),
                    collection_id=collection_id,
                    remaining=len(remaining),
                )
                raise ServiceError(
                    502,
                    "face_deletion_unverified",
                    "DeleteFaces completed but ListFaces still returns faces;"
                    " nothing was tombstoned. Retry.",
                    retryable=True,
                )
    except FaceIndexUnavailable:
        raise ServiceError(
            503,
            "face_index_unavailable",
            "Face deletion is temporarily unavailable; nothing was tombstoned."
            " Retry.",
            retryable=True,
        ) from None

    tombstoned = await store.tombstone_enrolments(ref)
    log.info("enrolment.deleted", user_ref=str(ref), enrolments=tombstoned)
    return Response(status_code=204)
