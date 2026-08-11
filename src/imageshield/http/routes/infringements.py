"""User feedback on a hit.

One endpoint. The proxy's report surface had nowhere to put "that is not me",
so a user looking at a match of their own face could read it and do nothing.

Two properties are worth stating before the code, because both are easy to
undo by accident.

**404 covers two different facts and must not distinguish them.** An
infringement that does not exist and one belonging to a different ``user_ref``
answer identically — same status, same code, same message, same body. The
difference is an enumeration oracle: a caller who can tell "not yours" from
"not there" can walk the id space and learn that a given infringement exists,
which for this data is a disclosure about somebody's abuse. The store puts
``user_ref`` in the ``WHERE`` clause rather than checking ownership afterwards,
so there is no branch here that could drift apart.

**``not_me`` is recorded and not acted on.** It writes a feedback row and sets
the infringement's status so the user stops seeing it. It does *not* adjust
identity vectors, suppress the domain, or feed banding — see
``search/feedback.py`` for why that restraint is the design.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends

from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_search_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import FeedbackRequest, FeedbackResponse
from imageshield.search.store import SearchStore

log = structlog.get_logger("imageshield.infringements")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@router.post("/infringements/{infringement_id}/feedback")
async def record_feedback(
    infringement_id: UUID,
    body: FeedbackRequest,
    store: SearchStore = Depends(get_search_store),
) -> FeedbackResponse:
    status = await store.record_feedback(infringement_id, body.user_ref, body.signal)
    if status is None:
        # Not there, or not theirs. One answer for both — see the module
        # docstring. Do not add a distinguishing message, a distinguishing
        # code, or a log line the caller can time.
        raise ServiceError(
            404,
            "infringement_not_found",
            "No such infringement for this user_ref.",
            retryable=False,
        )
    log.info(
        "infringement.feedback_recorded",
        infringement_id=str(infringement_id),
        signal=body.signal,
        status=status,
    )
    return FeedbackResponse(status=status)
