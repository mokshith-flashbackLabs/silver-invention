"""Cut each attributed subject's face out of the photo and hand it to the proxy.

**INVARIANTS #9 is untouched.** Nothing is persisted by this service: the crop
is made from bytes already in memory, pushed out through a proxy-minted
presigned PUT, and the buffer dies with the request. That is precisely what
hard rule 3 describes, and it is the same path the liveness ``ReferenceImage``
already takes — the same ``ObjectUploader``, deliberately, rather than a second
one that could drift.

**Pillow gains no extra call site.** ``crop_to_face`` already exists and is
already sanctioned (CLAUDE.md §2); this calls it again from the module that
owns the attribution sequence. Re-cropping rather than threading the search's
crop out through the provider interface costs one deterministic Pillow
operation and keeps a persistence concern out of an adapter whose job is a
Rekognition call.

THE RULE THAT MATTERS, and the one to leave alone:

    A subject whose crop does not reach the bucket gets NO SEED.
    Not a photo seed. No seed.

Falling back to the full photo would mean a transient S3 hiccup silently
reinstates the exact transmission this whole design exists to prevent, and it
would do it invisibly — the run still returns 200, the seed still exists, and
nothing anywhere says the query image now contains three other people. A
missing seed is recoverable: the next ``POST /v1/attribute`` for that photo
registers it. A face already sent to Hive is not recoverable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from imageshield.attribution.crop import CropTooSmall, crop_to_face
from imageshield.attribution.models import (
    AttributedFace,
    CropTarget,
    PlannedSeed,
)
from imageshield.attribution.seeds import (
    crop_seed,
    photo_seed,
    target_for,
    wants_crops,
)
from imageshield.liveness.models import UploadError
from imageshield.liveness.uploader import ObjectUploader
from imageshield.types import UserRef

log = structlog.get_logger("imageshield.attribution")

# crop_to_face writes JPEG at quality 95. The proxy presigns for exactly this
# content type, so a mismatch is a 403 from S3 rather than a stored object with
# the wrong header.
_CROP_CONTENT_TYPE = "image/jpeg"


@dataclass(frozen=True, slots=True)
class SkippedSeed:
    """A subject we meant to seed and did not. Recorded, never silent.

    The count of these is the difference between "nobody else was in the photo"
    and "we could not store their crop", and those must not be confusable — the
    second is worth retrying and the first is not.
    """

    user_ref: UserRef
    reason: str


async def plan_seeds(
    *,
    image: bytes,
    photo_ref: str,
    seed_owners: Sequence[tuple[UserRef, AttributedFace]],
    crop_targets: Sequence[CropTarget],
    faces_detected: int,
    uploader: ObjectUploader,
) -> tuple[tuple[PlannedSeed, ...], tuple[SkippedSeed, ...]]:
    """Decide each subject's seed, persisting crops where the run calls for one.

    ``image`` is the transcoded JPEG the searches ran against, not the original
    fetch — same bytes, same bbox, same function, so the crop here is identical
    to the crop that was searched.
    """
    if not wants_crops(crop_targets, faces_detected):
        # The caller sent no targets, or the photo has one face and the photo
        # therefore IS the subject's image. Unchanged behaviour.
        return tuple(photo_seed(ref, face, photo_ref) for ref, face in seed_owners), ()

    planned: list[PlannedSeed] = []
    skipped: list[SkippedSeed] = []
    for user_ref, face in seed_owners:
        target = target_for(crop_targets, user_ref)
        if target is None:
            # The proxy mints one target per candidate and every attributed
            # subject was a candidate, so this is a caller bug. It is still not
            # a reason to seed the photo: see the module docstring.
            skipped.append(SkippedSeed(user_ref=user_ref, reason="no_crop_target"))
            continue
        try:
            crop = crop_to_face(image, face.bbox)
        except CropTooSmall:
            # Should be unreachable: a face too small to crop is a face that was
            # never searched, so it was never attributed and never reached here.
            # Handled anyway, and handled as a skip rather than a fallback.
            skipped.append(SkippedSeed(user_ref=user_ref, reason="crop_too_small"))
            continue
        try:
            await uploader.put(target.crop_put_url, crop, content_type=_CROP_CONTENT_TYPE)
        except UploadError as exc:
            # The error text never carries the URL — the presigned query string
            # is a signature. Neither does this log line.
            log.warning(
                "attribution.crop_upload_failed",
                user_ref=str(user_ref),
                crop_ref=target.crop_ref,
                detail=str(exc),
            )
            skipped.append(SkippedSeed(user_ref=user_ref, reason="crop_upload_failed"))
            continue
        planned.append(crop_seed(user_ref, face, target.crop_ref))

    return tuple(planned), tuple(skipped)
