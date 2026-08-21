"""The subject's surface for one hit: feedback, the blurred preview, and (in
``subject_decide``'s route, below) the decision itself — spec 2026-08-21.

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
from fastapi import APIRouter, Depends, Query, Response

from imageshield.config import Config
from imageshield.http.auth import require_service_token
from imageshield.http.deps import (
    get_config,
    get_crop_client,
    get_preview_store,
    get_score_store,
    get_search_store,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import FeedbackRequest, FeedbackResponse
from imageshield.preview.client import CropUnavailable, FetcherCropClient
from imageshield.preview.store import PreviewStore
from imageshield.score.store import ScoreStore
from imageshield.search.store import SearchStore
from imageshield.types import parse_user_ref

log = structlog.get_logger("imageshield.infringements")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


def _not_found() -> ServiceError:
    # Not there, or not theirs (or invisible: quarantined/duplicate). One
    # answer for all — see the module docstring. Do not add a distinguishing
    # message, a distinguishing code, or a log line the caller can time.
    return ServiceError(
        404,
        "infringement_not_found",
        "No such infringement for this user_ref.",
        retryable=False,
    )


@router.post("/infringements/{infringement_id}/feedback")
async def record_feedback(
    infringement_id: UUID,
    body: FeedbackRequest,
    store: SearchStore = Depends(get_search_store),
    score_store: ScoreStore = Depends(get_score_store),
) -> FeedbackResponse:
    status = await store.record_feedback(infringement_id, body.user_ref, body.signal)
    if status is None:
        raise _not_found()
    log.info(
        "infringement.feedback_recorded",
        infringement_id=str(infringement_id),
        signal=body.signal,
        status=status,
    )
    try:
        await score_store.recompute(
            body.user_ref, cause_kind="feedback", cause_ref=str(infringement_id)
        )
    except Exception:  # deliberate: the trigger already committed; tick will heal
        log.warning(
            "score.recompute_failed", user_ref=str(body.user_ref), cause="feedback"
        )
    return FeedbackResponse(status=status)


@router.get("/infringements/{infringement_id}/preview")
async def preview(
    infringement_id: UUID,
    user_ref: UUID = Query(...),
    reveal: bool = Query(False),
    store: PreviewStore = Depends(get_preview_store),
    crop_client: FetcherCropClient = Depends(get_crop_client),
    cfg: Config = Depends(get_config),
) -> Response:
    """The subject's blurred face crop — the only path by which a hit's pixels
    ever reach a user (spec 2026-08-21 §4). Blurred unless ``reveal=true`` (the
    app's per-item explicit tap, INVARIANTS #23); every render is audited
    before it happens (#31) and rate-ceilinged per user (#32); the JPEG is
    streamed with ``no-store`` and persisted nowhere (#9/#10). The bbox and
    image_url are looked up server-side and never reach the client (#13)."""
    subject = parse_user_ref(user_ref)
    target = await store.target(infringement_id, subject)
    if target is None:
        raise _not_found()
    if target.image_url is None or target.bbox is None:
        # Only reachable once ownership passed, so the distinct code leaks
        # nothing cross-user. The app falls back to domain + "no preview";
        # the subject can still decide.
        raise ServiceError(
            404,
            "preview_unavailable",
            "No renderable crop for this hit yet.",
            retryable=False,
        )
    if await store.renders_last_24h(subject) >= cfg.preview_daily_render_ceiling:
        raise ServiceError(
            429,
            "preview_rate_limited",
            "Preview render ceiling reached for this user.",
            retryable=True,
        )
    # Audit BEFORE the render (INVARIANTS #31): a render that then fails
    # upstream still shows an attempt, and still counts against the ceiling.
    await store.record_render(subject, infringement_id, reveal=reveal)
    try:
        content = await crop_client.crop(
            url=target.image_url, bbox=target.bbox, blur=not reveal
        )
    except CropUnavailable as exc:
        if exc.unrenderable:
            raise ServiceError(
                404,
                "preview_unavailable",
                "No renderable crop for this hit.",
                retryable=False,
            ) from exc
        raise ServiceError(
            502,
            "preview_unavailable_upstream",
            "Crop render failed upstream.",
            retryable=True,
        ) from exc
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, private"},
    )
