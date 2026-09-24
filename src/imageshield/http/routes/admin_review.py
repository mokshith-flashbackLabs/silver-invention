"""Review queue — the console surface for the human-only confirm gate
(Task 15; migration 0021; INVARIANTS #19).

Same posture as ``admin_providers.py`` and ``admin_threat_events.py``: both
tokens required at router level. ``GET /next`` and ``GET /queue`` are plain
reads; ``POST /{task_id}/decision`` is the only write in this file and it is
also the only thing anywhere in this codebase that can move an infringement
into ``confirmed`` or ``rejected`` — see ``imageshield.review.store`` for the
transaction.

"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Response

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_review_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewStatsResponse,
    ReviewTaskResponse,
)
from imageshield.review.store import ReviewStore

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


@router.get("/subject-decisions")
async def subject_decisions(
    limit: int = Query(50, ge=1, le=500),
    store: ReviewStore = Depends(get_review_store),
) -> dict[str, list[dict[str, Any]]]:
    """The observer feed (spec 2026-08-21 §6): what subjects decided about
    their own hits — metadata only, newest first. Explicit-severity
    confirmations are the takedown-campaign candidates."""
    return {"decisions": list(await store.subject_decisions(limit=limit))}


@router.get("/open-hits")
async def open_hits(
    limit: int = Query(50, ge=1, le=500),
    store: ReviewStore = Depends(get_review_store),
) -> dict[str, list[dict[str, Any]]]:
    """Every hit still awaiting the subject's answer. The control room always
    sees THAT a person has a hit (owner requirement, 2026-08-21) — what it
    never sees is the hit's pixels."""
    return {"hits": list(await store.open_hits(limit=limit))}


# The stats window's default, 30 days. A function rather than a module
# constant: a constant is evaluated at import, so on a long-lived process the
# window would silently stop moving.
_DEFAULT_STATS_DAYS = 30


def _default_since() -> datetime:
    return datetime.now(UTC) - timedelta(days=_DEFAULT_STATS_DAYS)


@router.get("/stats")
async def verdict_stats(
    since: datetime | None = Query(None),
    store: ReviewStore = Depends(get_review_store),
) -> ReviewStatsResponse:
    """How often the machine was right, by severity, against the subject, and
    by reviewer (2026-09-14).

    This route is the reason `review_verdicts` exists: face matching runs on
    Rekognition today and is being replaced, and the swap needs a measured
    false-positive rate rather than an impression. It lives on the REVIEW
    router rather than beside the feed in ``admin_hits.py`` because it is a
    statement about reviewing, not about hits.

    A rate whose denominator is zero comes back ``null``. Never 0.0 — "we
    measured no false positives" and "we measured nothing" are different
    claims, and only one of them should be allowed anywhere near a decision
    about replacing a matcher.
    """
    return ReviewStatsResponse(
        **await store.verdict_stats(since=since if since is not None else _default_since())
    )


@router.post("/{task_id}/decision")
async def decide(
    task_id: UUID,
    body: ReviewDecisionRequest,
    store: ReviewStore = Depends(get_review_store),
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
    return ReviewDecisionResponse(
        infringement_id=outcome.infringement_id,
        user_ref=outcome.user_ref,
        decision=outcome.decision,
        severity=outcome.severity,
    )
