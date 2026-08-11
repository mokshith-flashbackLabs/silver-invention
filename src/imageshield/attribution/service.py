"""Orchestration: photo in, faces resolved, seeds registered.

The sequence, and why each step is where it is:

1. Fetch the photo through the proxy's presigned GET. Rekognition accepts
   ``Bytes`` or ``S3Object`` and nothing else — there is no URL form — and this
   service holds no S3 credentials, so bytes through the presigned URL is the
   only route. They live in a local variable and are discarded, exactly as the
   enrolment path already does with the liveness ReferenceImage.
2. DetectFaces. No faces is a 200 with an empty list and a COMPLETED run, not
   an error: "there is nobody in this photo" is a fine answer.
3. Per face, search the collection, then discard every match outside
   ``candidate_refs`` (``resolve.py``). Highest surviving score wins.
4. One seed per distinct attributed person — not per face, so a photo showing
   someone twice is still one seed for them.
5. Run + faces + seeds in ONE transaction.

An unattributed face is the common case and is handled as ordinary throughout:
not logged as a warning, not counted as an error, not surfaced to the proxy as
a problem. Most faces in most photos belong to people who are not enrolled, and
that is exactly what the face-level rule intends.
"""

from __future__ import annotations

import structlog

from imageshield.attribution.models import (
    AttributedFace,
    AttributionOutcome,
    AttributionUnavailable,
)
from imageshield.attribution.provider import FaceAttributionProvider, PhotoFetcher
from imageshield.attribution.resolve import distinct_attributed, resolve_face
from imageshield.attribution.store import AttributionStore
from imageshield.types import UserRef

log = structlog.get_logger("imageshield.attribution")


async def attribute_photo(
    *,
    photo_ref: str,
    requested_by: UserRef,
    candidate_refs: tuple[UserRef, ...],
    presigned_get_url: str,
    collection_id: str,
    match_threshold: float,
    max_candidates: int,
    fetcher: PhotoFetcher,
    provider: FaceAttributionProvider,
    store: AttributionStore,
) -> AttributionOutcome:
    model_id = provider.model_id
    try:
        image = await fetcher.fetch(presigned_get_url)
        detected = await provider.detect_faces(image)
    except AttributionUnavailable as exc:
        await store.record_failed_run(
            photo_ref=photo_ref,
            requested_by=requested_by,
            candidate_count=len(candidate_refs),
            match_threshold=match_threshold,
            max_candidates=max_candidates,
            model_id=model_id,
            error_detail=str(exc),
        )
        raise

    faces: list[AttributedFace] = []
    try:
        for face in detected:
            matches = await provider.search_face(
                image,
                face,
                collection_id=collection_id,
                match_threshold=match_threshold,
                max_candidates=max_candidates,
            )
            # The filter is applied HERE, never inside the provider: the
            # discard rule is one pure function with one set of tests rather
            # than a property of whichever adapter happens to be wired in.
            faces.append(resolve_face(face, matches, candidate_refs))
    except AttributionUnavailable as exc:
        await store.record_failed_run(
            photo_ref=photo_ref,
            requested_by=requested_by,
            candidate_count=len(candidate_refs),
            match_threshold=match_threshold,
            max_candidates=max_candidates,
            model_id=model_id,
            error_detail=str(exc),
        )
        raise

    seed_owners = distinct_attributed(tuple(faces))
    outcome = await store.record_run(
        photo_ref=photo_ref,
        requested_by=requested_by,
        candidate_count=len(candidate_refs),
        match_threshold=match_threshold,
        max_candidates=max_candidates,
        model_id=model_id,
        faces=tuple(faces),
        seed_owners=seed_owners,
    )
    log.info(
        "attribution.completed",
        run_id=str(outcome.run_id),
        photo_ref=photo_ref,
        faces_detected=len(faces),
        # Counted, not warned about. An unattributed face is the expected
        # majority, and logging it as a problem would train everyone to ignore
        # the log.
        faces_attributed=sum(1 for f in faces if f.resolved_user_ref is not None),
        seeds_registered=len(outcome.seeds),
        model_id=model_id,
    )
    return outcome
