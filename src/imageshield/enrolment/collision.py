"""The enrolment collision gate — the ONE face search in the enrolment path.

INVARIANTS #1, as reworded 2026-09-22: a search here may REFUSE an enrolment
and may never ASSIGN, mint, merge or overwrite a ``user_ref``. This module is
built so that it cannot: it imports no store, no IndexFaces, nothing that
carries a session or an enrolment, and its only output is "this frame already
belongs to a different user_ref" or None. ``tests/test_boundaries.py``
asserts those imports structurally.

Why it exists: an on-device household member's liveness runs on the OWNER's
phone. Whoever is in front of the camera is indexed as the member — nothing
asked whether that face already had an identity. The old fear (a score
choosing who somebody is) is real and stays forbidden; a score refusing to
duplicate an identity that already exists is the opposite failure's cure.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from imageshield.config import Config
from imageshield.enrolment.models import Collision, FaceSearchResult
from imageshield.types import UserRef, parse_user_ref

log = structlog.get_logger("imageshield.enrolment.collision")


class FaceSearcher(Protocol):
    """The slice of the face index this module is allowed to see: search, and
    only search. RekognitionFaceIndex satisfies it structurally; nothing here
    can reach the indexing call because nothing here names it."""

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult: ...


async def collision_check(
    searcher: FaceSearcher,
    cfg: Config,
    own_ref: UserRef,
    image_bytes: bytes,
    *,
    candidates: frozenset[UserRef] | None = None,
) -> Collision | None:
    """Return the strongest match belonging to somebody ELSE, or None.

    ``candidates`` is the household, named by the proxy. Rekognition cannot
    scope a search, so the household is a RESULT filter — the same load-bearing
    move attribution makes (INVARIANTS #1a): a match outside the list is
    discarded before it can influence anything. A lookalike in another
    household never blocks an enrolment and never leaks that such a face
    exists. ``None`` means no list was sent and the whole collection counts —
    the strict fallback, so a missing field can never WEAKEN the gate.

    - ``own_ref`` hits are ignored: re-enrolment must keep working.
    - An ExternalImageId that does not parse as a UserRef is discarded: we set
      every one ourselves (INVARIANTS #6), so a stray value is something we did
      not put there — a reason to ignore that hit, not to refuse a person.
    - A provider failure propagates (FaceIndexUnavailable): the route fails
      CLOSED with 503 rather than skipping the check.

    Neither user_ref is logged here. The matched one is what the response must
    never carry, and a log line is a response to whoever reads logs.
    """
    found = await searcher.search_face(
        collection_id=cfg.identity_collection,
        image_bytes=image_bytes,
        threshold=cfg.enrolment_collision_threshold,
        max_faces=cfg.enrolment_collision_max_faces,
    )
    best: tuple[float, UserRef] | None = None
    for hit in found.hits:
        try:
            ref = parse_user_ref(hit.external_image_id)
        except ValueError:
            continue
        if ref == own_ref:
            continue
        if candidates is not None and ref not in candidates:
            continue
        if best is None or hit.similarity > best[0]:
            best = (hit.similarity, ref)
    if best is None:
        return None
    similarity, matched = best
    log.info(
        "enrolment.collision_detected",
        similarity=similarity,
        threshold=cfg.enrolment_collision_threshold,
        model_id=found.model_id,
    )
    return Collision(
        matched_user_ref=matched,
        similarity=similarity,
        model_id=found.model_id,
        threshold_used=cfg.enrolment_collision_threshold,
    )
