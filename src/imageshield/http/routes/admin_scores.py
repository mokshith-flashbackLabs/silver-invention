"""Protection score + journal admin reads (Task 15).

One route: a console needs to see what a person's score is and how it got
there, and the journal (``score_events``) is the only place that story lives
— ``protection_scores`` is materialised state with no history of its own
(``imageshield.score.store`` module docstring). Read-only; nothing here can
move a score, which only ``ScoreStore.recompute`` ever does.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_score_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import ScoreDetailResponse, ScoreEventItem, ScoreResponse
from imageshield.score.store import ScoreStore
from imageshield.types import UserRef

router = APIRouter(
    prefix="/v1/admin/scores",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


@router.get("/{user_ref}")
async def get_score(
    user_ref: UUID,
    store: ScoreStore = Depends(get_score_store),
) -> ScoreResponse:
    ref = UserRef(user_ref)
    score = await store.get_score(ref)
    if score is None:
        raise ServiceError(
            404,
            "score_not_found",
            "No protection score for this user_ref — nothing has been computed yet.",
            retryable=False,
        )
    events = await store.list_events(ref, limit=50)
    return ScoreResponse(
        score=ScoreDetailResponse(**score),
        events=[ScoreEventItem(**event) for event in events],
    )
