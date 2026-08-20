"""Review queue — the console surface for the human-only confirm gate
(Task 15; migration 0021; INVARIANTS #19).

Same posture as ``admin_providers.py`` and ``admin_threat_events.py``: both
tokens required at router level. ``GET /next`` and ``GET /queue`` are plain
reads; ``POST /{task_id}/decision`` is the only write in this file and it is
also the only thing anywhere in this codebase that can move an infringement
into ``confirmed`` or ``rejected`` — see ``imageshield.review.store`` for the
transaction.

A confirmed or rejected decision changes exposure, so it must feed the
protection score the same way a feedback signal or a threat event does
(``infringements.py``, ``admin_threat_events.py``): the swallow-and-log
recompute wrapper, never allowed to change this request's response.
``uncertain`` changes nothing on the infringement, so it recomputes nothing —
recomputing on a no-op write would just be a wasted read.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Response

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_review_store, get_score_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewTaskResponse,
)
from imageshield.review.store import ReviewStore
from imageshield.score.store import ScoreStore

log = structlog.get_logger("imageshield.review")

router = APIRouter(
    prefix="/v1/admin/review",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


def _not_found(task_id: UUID) -> ServiceError:
    return ServiceError(
        404,
        "review_task_not_found",
        f"No pending review task {task_id!r}.",
        retryable=False,
    )


@router.get("/next", response_model=None)
async def next_task(
    store: ReviewStore = Depends(get_review_store),
) -> ReviewTaskResponse | Response:
    task = await store.next_task()
    if task is None:
        # 204: an empty queue is a real, expected state, not an error — a
        # console polling this route should not treat it as a failure. A bare
        # Response bypasses pydantic serialisation so 204 carries no body,
        # rather than a literal `null` FastAPI would otherwise write for one.
        return Response(status_code=204)
    return ReviewTaskResponse(**task)


@router.get("/queue")
async def queue_depth(store: ReviewStore = Depends(get_review_store)) -> dict[str, int]:
    return await store.queue_depth()


@router.post("/{task_id}/decision")
async def decide(
    task_id: UUID,
    body: ReviewDecisionRequest,
    store: ReviewStore = Depends(get_review_store),
    score_store: ScoreStore = Depends(get_score_store),
) -> ReviewDecisionResponse:
    outcome = await store.decide(
        task_id, decision=body.decision, operator=body.operator, severity=body.severity
    )
    if outcome is None:
        raise _not_found(task_id)
    log.info(
        "review.decided",
        task_id=str(task_id),
        infringement_id=str(outcome.infringement_id),
        decision=outcome.decision,
        operator=body.operator,
    )
    if outcome.decision in ("confirmed", "rejected"):
        try:
            await score_store.recompute(
                outcome.user_ref,
                cause_kind="review_decision",
                cause_ref=str(outcome.infringement_id),
            )
        except Exception:  # deliberate: the decision already committed; tick will heal
            log.warning(
                "score.recompute_failed",
                user_ref=str(outcome.user_ref),
                cause="review_decision",
            )
    return ReviewDecisionResponse(
        infringement_id=outcome.infringement_id,
        user_ref=outcome.user_ref,
        decision=outcome.decision,
        severity=outcome.severity,
    )
