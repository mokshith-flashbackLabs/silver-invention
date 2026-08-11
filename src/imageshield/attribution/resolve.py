"""Which candidate, if any, a face resolves to. Pure — no I/O, no provider.

This module is the load-bearing half of INVARIANTS #1a, and it is separate
from the provider call so it can be read and tested without one.

**Household scoping is a RESULT FILTER, not a search parameter.**
``SearchFacesByImage`` accepts only ``CollectionId``, ``Image``,
``FaceMatchThreshold``, ``MaxFaces`` and ``QualityFilter`` — verified against
botocore's own service model, not from memory. There is no way to restrict the
search set to a candidate list. So the search runs against the whole of
``identity-v1`` and every match outside ``candidate_refs`` is discarded HERE,
before it can influence anything.

That filter is the only thing standing between "a stranger outranked the
household member" and "person A's photo became person B's monitored seed". It
is application code, not a constraint, which is exactly why it is isolated,
pure, and tested with a planted non-candidate that outscores the real one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from imageshield.attribution.models import (
    AttributedFace,
    DetectedFace,
    FaceMatch,
)
from imageshield.types import UserRef, parse_user_ref


def resolve_face(
    face: DetectedFace,
    matches: Sequence[FaceMatch],
    candidate_refs: Iterable[UserRef],
) -> AttributedFace:
    """Attribute one face, or leave it unattributed.

    Highest-scoring SURVIVING candidate wins. "Surviving" is the whole point:
    the winner is chosen after the filter, never before, so a higher-scoring
    non-candidate cannot displace a legitimate one — it is simply not in the
    running.
    """
    allowed = set(candidate_refs)
    surviving = [
        (match, ref)
        for match in matches
        if (ref := _as_candidate(match.external_image_id, allowed)) is not None
    ]
    if not surviving:
        # The common case, and a first-class success. Most faces in most photos
        # belong to people who are not enrolled.
        return AttributedFace(
            face_index=face.face_index,
            bbox=face.bbox,
            detect_confidence=face.detect_confidence,
            resolved_user_ref=None,
            match_score=None,
        )
    best_match, best_ref = max(surviving, key=lambda pair: pair[0].similarity)
    return AttributedFace(
        face_index=face.face_index,
        bbox=face.bbox,
        detect_confidence=face.detect_confidence,
        resolved_user_ref=best_ref,
        match_score=best_match.similarity,
    )


def _as_candidate(external_image_id: str, allowed: set[UserRef]) -> UserRef | None:
    """The ExternalImageId as a candidate ``user_ref``, or ``None`` to discard.

    An unparseable value is discarded rather than raised on. We set every
    ExternalImageId in this collection ourselves (INVARIANTS #6), so a
    non-UUID means the collection holds something we did not put there — which
    is a reason to ignore that match, not to fail a user's whole photo.
    """
    try:
        ref = parse_user_ref(external_image_id)
    except ValueError:
        return None
    return ref if ref in allowed else None


def distinct_attributed(
    faces: Sequence[AttributedFace],
) -> tuple[tuple[UserRef, AttributedFace], ...]:
    """One (person, face) pair per distinct attributed person, best score first.

    A photo showing the same person twice — a mirror, a poster on the wall — is
    still ONE seed for them. The seed is the photo, and registering it twice
    would double their scan cost for no extra coverage. The highest-scoring
    face is kept as the provenance link because it is the strongest evidence
    that the person is in the picture.
    """
    best: dict[UserRef, AttributedFace] = {}
    for face in faces:
        ref = face.resolved_user_ref
        if ref is None:
            continue
        current = best.get(ref)
        if current is None or (face.match_score or 0.0) > (current.match_score or 0.0):
            best[ref] = face
    return tuple(best.items())
