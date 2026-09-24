"""The reviewer hit feed, verdicts, and the audited operator preview
(2026-09-14; spec
``docs/superpowers/specs/2026-09-14-auto-confirm-and-reviewer-feed-design.md``).

**Why this file exists.** Face matching runs on Rekognition today and the team
is replacing it with its own models. A false-positive rate cannot be measured
without a human looking at real hits and recording whether the machine was
right — and until now the control room could see a queue depth and one task at
a time, could not list a person's hits at all, and could not see the image.

Three things, in the order they matter:

1. ``GET /v1/admin/hits`` — every hit a reviewer may look at, keyset-paged,
   filterable. Quarantined hits are excluded in the store's ``WHERE``, not
   here, so no filter combination can reach one.
2. ``POST /v1/admin/hits/{id}/verdict`` — the LABEL. It writes
   ``review_verdicts`` and an audit row and moves nothing else (owner decision
   D6); ``POST /v1/admin/review/{task_id}/decision`` remains the only override.
3. ``GET /v1/admin/infringements/{id}/preview`` — the same blurred render the
   subject would get, audited per view with the operator's name and ceilinged
   per operator (owner decision D5).

**On the preview, and INVARIANTS #19/#23.** #19's "staff never see hit
imagery" clause is amended by this file and by nothing else: staff see the
*identical* render a subject would, because judging a false positive without
seeing the face is not review, it is guessing. #23 is UNCHANGED, and the
reason is structural rather than a promise — this route calls
``crop_client.crop`` with the same arguments the subject route builds, so
there is no operator render mode to get wrong. ``blur=not reveal``, and on
the fetcher's side ``blur=False`` means "sharpen the face box", never "return
the frame sharp".

Same posture as every other admin router: both tokens at router level, so a
new route added to this file is guarded structurally rather than by memory.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Response

from imageshield.config import Config
from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import (
    get_config,
    get_crop_client,
    get_preview_store,
    get_review_store,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    AdminHitItem,
    AdminHitsResponse,
    HitFilterConfirmState,
    ReviewSeverity,
    ReviewVerdictRequest,
    ReviewVerdictResponse,
)
from imageshield.preview.client import CropUnavailable, FetcherCropClient
from imageshield.preview.store import PreviewStore
from imageshield.review.store import ReviewStore
from imageshield.types import UserRef

log = structlog.get_logger("imageshield.admin_hits")

router = APIRouter(
    prefix="/v1/admin",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)

DEFAULT_HITS_LIMIT = 50
MAX_HITS_LIMIT = 200


def _encode_cursor(first_seen_at: datetime, infringement_id: UUID) -> str:
    """The keyset position, opaque to the client.

    Opaque so that changing the sort key later is a server change rather than
    a client one — and base64 rather than raw text so nobody is tempted to
    build one by hand out of a timestamp they saw in a payload.
    """
    raw = f"{first_seen_at.isoformat()}|{infringement_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """A malformed cursor is a 422 with its own code — never a 500, and never
    silently ignored.

    Ignoring it is the dangerous option: a reviewer paging through a feed
    would silently restart at page one and read the first fifty hits over and
    over, believing they had seen the tail.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, _, identifier = raw.partition("|")
        return datetime.fromisoformat(timestamp), UUID(identifier)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ServiceError(
            422,
            "invalid_cursor",
            "The `cursor` parameter is not a cursor this endpoint issued.",
            retryable=False,
        ) from exc


def _not_found() -> ServiceError:
    # Reuses the SUBJECT surface's code deliberately: it is the same fact
    # ("no such hit here"), and a reviewer-specific code would be a second
    # vocabulary for one condition. A quarantined hit answers this too — it is
    # excluded from every surface, and escalation out of a quarantine is the
    # manual legal process in docs/OPERATIONS.md.
    return ServiceError(
        404,
        "infringement_not_found",
        "No such infringement.",
        retryable=False,
    )


@router.get("/hits")
async def list_hits(
    limit: int = Query(DEFAULT_HITS_LIMIT, ge=1, le=MAX_HITS_LIMIT),
    cursor: str | None = Query(None),
    severity: ReviewSeverity | None = Query(None),
    confirm_state: HitFilterConfirmState | None = Query(None),
    user_ref: UUID | None = Query(None),
    since: datetime | None = Query(None),
    store: ReviewStore = Depends(get_review_store),
) -> AdminHitsResponse:
    """Every hit a reviewer may look at, newest first.

    Filters compose and every one is optional. What is NOT optional is the
    exclusion of quarantined hits — that lives in the store's ``WHERE``, so no
    combination of parameters here can reach one.
    """
    after = _decode_cursor(cursor) if cursor is not None else None
    page = await store.list_hits(
        limit=limit,
        after=after,
        severity=severity,
        confirm_state=confirm_state,
        user_ref=UserRef(user_ref) if user_ref is not None else None,
        since=since,
    )
    hits = [AdminHitItem(**hit) for hit in page.hits]
    next_cursor = (
        _encode_cursor(hits[-1].first_seen_at, hits[-1].infringement_id)
        if page.has_more and hits
        else None
    )
    return AdminHitsResponse(hits=hits, next_cursor=next_cursor)


@router.post("/hits/{infringement_id}/verdict", status_code=201)
async def record_verdict(
    infringement_id: UUID,
    body: ReviewVerdictRequest,
    store: ReviewStore = Depends(get_review_store),
) -> ReviewVerdictResponse:
    """Record "was the machine right about this hit".

    A verdict changes no user-facing fact — not the hit's state, not the
    person's exposure — so there is nothing to recompute. No write on this
    admin surface recomputes a score any more: the protection score was
    removed (spec 2026-09-24). That is the whole of decision D6 expressed in
    code.
    """
    record = await store.record_verdict(
        infringement_id,
        operator=body.operator,
        verdict=body.verdict,
        note=body.note,
    )
    if record is None:
        raise _not_found()
    log.info(
        "review.verdict_recorded",
        infringement_id=str(infringement_id),
        verdict=record.verdict,
        operator=body.operator,
        machine_severity=record.machine_severity,
    )
    return ReviewVerdictResponse(**record.model_dump())


@router.get("/infringements/{infringement_id}/preview")
async def operator_preview(
    infringement_id: UUID,
    operator: str = Query(..., min_length=1, max_length=64),
    reveal: bool = Query(False),
    store: PreviewStore = Depends(get_preview_store),
    crop_client: FetcherCropClient = Depends(get_crop_client),
    cfg: Config = Depends(get_config),
) -> Response:
    """The reviewer's view of a hit — byte-for-byte the subject's render.

    Owner decision D5: the same fetcher call, the same whole-frame blur, the
    same reveal-sharpens-only-the-face behaviour. There is no operator render
    mode and no request parameter that returns a sharp frame (INVARIANTS #23,
    unchanged and unchangeable from here — the flag this route passes is the
    one the subject route passes).

    **The order of operations is load-bearing.** Resolve, refuse the
    unrenderable, check the ceiling, AUDIT, then render — the audit lands
    before the render (INVARIANTS #31), so a render that then fails upstream
    still shows an attempt and still counts. A refusal is not an attempt and
    writes nothing.
    """
    target = await store.operator_target(infringement_id)
    if target is None:
        raise _not_found()
    if target.image_url is None or target.bbox is None:
        raise ServiceError(
            404,
            "preview_unavailable",
            "No renderable crop for this hit.",
            retryable=False,
        )
    if await store.operator_renders_last_24h(operator) >= (
        cfg.review_operator_daily_render_ceiling
    ):
        # INVARIANTS #32, the operator half. The abuse case is a compromised
        # operator account replayed as a browsing console over other people's
        # abuse imagery; the audit records it and this stops it. No audit row
        # is written for a refusal — nothing was rendered.
        raise ServiceError(
            429,
            "preview_rate_limited",
            "Preview render ceiling reached for this operator.",
            retryable=True,
        )
    await store.record_operator_render(operator, target.user_ref, infringement_id, reveal=reveal)
    try:
        content = await crop_client.crop(url=target.image_url, bbox=target.bbox, blur=not reveal)
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
