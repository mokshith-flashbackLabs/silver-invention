"""Subject eligibility read surface (step 8).

One route. It exists so the proxy can tell a household seat holder *why* a
minor's monitoring shows nothing, rather than presenting an empty report — an
empty report reads as "no matches", and for a subject nobody searched that is a
false reassurance (CLAUDE.md §4 #26 applied to a case where the corpus was
never even queried).

404 rather than a synthesised "eligible: unknown": a ``user_ref`` we hold no
subject row for has not enrolled here, and inventing an answer for it would let
the proxy treat "never enrolled" and "adult, eligible" as the same state.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_subject_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import SubjectResponse
from imageshield.subjects.store import SubjectStore
from imageshield.types import UserRef

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@router.get("/subjects/{user_ref}")
async def get_subject(
    user_ref: UUID,
    store: SubjectStore = Depends(get_subject_store),
) -> SubjectResponse:
    subject = await store.get_subject(UserRef(user_ref))
    if subject is None:
        raise ServiceError(
            404,
            "subject_unknown",
            "No subject record for this user_ref — nothing has enrolled it.",
            retryable=False,
        )
    return SubjectResponse(
        discovery_eligible=subject.discovery_eligible,
        eligibility_reason=subject.eligibility_reason,
    )
