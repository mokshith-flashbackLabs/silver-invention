"""``POST /v1/attribute`` — detect faces, attribute them, register seeds.

``match_threshold`` and ``max_candidates`` come from CONFIG, not the request
body. The task brief has them as request fields; taking them from the caller
would let a per-request value silently override the one place INVARIANTS #1b
says a threshold may live, and would make ``ATTRIBUTION_MAX_CANDIDATES >= 2``
— refused at boot precisely because its failure is invisible — bypassable by
sending 1. Both values are still recorded on the run, which is what the brief
actually needs them for: a later retune must not make historical attributions
uninterpretable.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from imageshield.attribution.models import AttributionUnavailable
from imageshield.attribution.provider import FaceAttributionProvider, PhotoFetcher
from imageshield.attribution.service import attribute_photo
from imageshield.attribution.store import AttributionStore
from imageshield.config import Config
from imageshield.http.auth import require_service_token
from imageshield.http.deps import (
    get_attribution_provider,
    get_attribution_store,
    get_config,
    get_photo_fetcher,
    get_score_store,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    AttributedFaceItem,
    AttributeRequest,
    AttributeResponse,
    RegisteredSeedItem,
)
from imageshield.score.store import ScoreStore

log = structlog.get_logger("imageshield.attribution")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@router.post("/attribute")
async def attribute(
    body: AttributeRequest,
    cfg: Config = Depends(get_config),
    fetcher: PhotoFetcher = Depends(get_photo_fetcher),
    provider: FaceAttributionProvider = Depends(get_attribution_provider),
    store: AttributionStore = Depends(get_attribution_store),
    score_store: ScoreStore = Depends(get_score_store),
) -> AttributeResponse:
    try:
        outcome = await attribute_photo(
            photo_ref=body.photo_ref,
            requested_by=body.requested_by,
            candidate_refs=tuple(body.candidate_refs),
            presigned_get_url=body.presigned_get_url,
            collection_id=cfg.identity_collection,
            match_threshold=cfg.attribution_match_threshold,
            max_candidates=cfg.attribution_max_candidates,
            fetcher=fetcher,
            provider=provider,
            store=store,
        )
    except AttributionUnavailable as exc:
        # The run is recorded as 'failed' by the service before this fires, so
        # "we could not look" stays distinguishable from "we looked and matched
        # nobody" — only one of them is worth retrying.
        raise ServiceError(
            503,
            "attribution_unavailable",
            "Face attribution is temporarily unavailable; retry.",
            retryable=True,
        ) from exc

    for seed_user_ref in dict.fromkeys(seed.user_ref for seed in outcome.seeds):
        try:
            await score_store.recompute(seed_user_ref, cause_kind="seed_registered")
        except Exception:  # deliberate: the trigger already committed; tick will heal
            log.warning(
                "score.recompute_failed",
                user_ref=str(seed_user_ref),
                cause="seed_registered",
            )

    return AttributeResponse(
        run_id=outcome.run_id,
        faces=[
            AttributedFaceItem(
                face_index=face.face_index,
                bbox=face.bbox.as_dict(),
                detect_confidence=face.detect_confidence,
                resolved_user_ref=face.resolved_user_ref,
                match_score=face.match_score,
            )
            for face in outcome.faces
        ],
        seeds_registered=[
            RegisteredSeedItem(user_ref=seed.user_ref, seed_id=seed.seed_id)
            for seed in outcome.seeds
        ],
    )
