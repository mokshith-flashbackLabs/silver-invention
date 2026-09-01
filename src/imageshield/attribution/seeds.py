"""What each attributed subject gets searched with (spec 2026-08-31).

Until this module existed, every attributed subject's seed was the whole photo.
On a group photo that means a person in frame who never consented — a household
member with monitoring off, one who has not enrolled, a passer-by — has their
face sent to Hive and Google on every scan cycle, indefinitely. The face-level
seed gate stops a stranger *becoming a monitored subject*; it says nothing about
their face being *transmitted*. Cropping to the subject is what removes them
from the query.

Keeping the full photo alongside a crop would not help — the face still ships —
so this is a replacement, not an addition.

The decision is pure and lives here; the PUT lives in :mod:`crop_upload`. That
split is what lets the rules below be tested without a network, and the rules
are the part that is easy to get subtly and invisibly wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

from imageshield.attribution.models import (
    AttributedFace,
    CropTarget,
    PlannedSeed,
)
from imageshield.types import UserRef

# The corpus stays legible: nothing else distinguishes "the photo they gave us"
# from "a region we cut out of it", and that is the first question anyone asks
# when calibration looks odd. Reusing 'user_supplied' would make it
# unanswerable (spec §4).
SEED_KIND_PHOTO = "user_supplied"
SEED_KIND_CROP = "face_crop"

# Below this, cropping buys nothing: the photo IS the subject's image, and
# there is no second person in it to remove.
MIN_FACES_FOR_CROP = 2


def wants_crops(crop_targets: Sequence[CropTarget], faces_detected: int) -> bool:
    """Is this a run where seeds should be crops rather than the photo?

    Two conditions, and both are about the caller and the photo rather than
    about anything we discovered: the proxy asked (it minted targets), and the
    photo has somebody else in it.
    """
    return bool(crop_targets) and faces_detected >= MIN_FACES_FOR_CROP


def photo_seed(user_ref: UserRef, face: AttributedFace, photo_ref: str) -> PlannedSeed:
    """The pre-2026-08-31 behaviour, unchanged and still correct in two cases:
    a caller that sent no targets, and a photo with one face in it."""
    return PlannedSeed(
        user_ref=user_ref,
        face=face,
        source_object_ref=photo_ref,
        seed_kind=SEED_KIND_PHOTO,
    )


def crop_seed(user_ref: UserRef, face: AttributedFace, crop_ref: str) -> PlannedSeed:
    """The seed for a subject whose crop reached the proxy's bucket.

    ``source_object_ref`` is the crop's own key, so the search dispatches the
    crop and the group photo is never fetched by a provider again.
    """
    return PlannedSeed(
        user_ref=user_ref,
        face=face,
        source_object_ref=crop_ref,
        seed_kind=SEED_KIND_CROP,
        crop_object_ref=crop_ref,
    )


def target_for(crop_targets: Sequence[CropTarget], user_ref: UserRef) -> CropTarget | None:
    """The target minted for this subject, if the proxy minted one.

    ``None`` on a crop run is NOT a reason to seed the photo instead — see the
    module docstring in :mod:`crop_upload` and spec §5. The proxy mints one per
    candidate and every attributed subject was a candidate, so this is a caller
    bug rather than a normal branch; the safe response to a caller bug here is
    to register nothing.
    """
    for target in crop_targets:
        if target.user_ref == user_ref:
            return target
    return None
