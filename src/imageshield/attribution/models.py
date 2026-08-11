"""Domain types for attribution.

Three quantities live here and they are deliberately never the same field:

- ``detect_confidence`` — "this region is a face". About pixels.
- ``similarity`` / ``match_score`` — "this face is that person". About identity.
- the *decision* — which candidate won, or none.

Conflating the first two is how a confident detection of a stranger reads as a
confident identification of a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from imageshield.types import UserRef


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Normalised 0-1, as Rekognition reports it. Stored for EVERY detected
    face, attributed or not: it is the provenance of a decision, and the proxy
    renders boxes from it."""

    x: float
    y: float
    w: float
    h: float

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True, slots=True)
class DetectedFace:
    """One face found in the photo. Not yet attributed to anyone."""

    face_index: int
    bbox: BoundingBox
    detect_confidence: float


@dataclass(frozen=True, slots=True)
class FaceMatch:
    """One candidate the collection returned for a face.

    ``external_image_id`` is the value WE set at enrolment — a ``user_ref`` and
    nothing else (INVARIANTS #6). It is not a name, not a contact detail, and
    not something the collection invented.

    (The word this paragraph is avoiding is grepped for across this package by
    the step-9 gate, and prose describing a boundary must not read as a breach
    of it — the same reason the DocuSeal tripwire strips docstrings.)
    """

    external_image_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class AttributedFace:
    """A detected face plus the decision about it.

    ``resolved_user_ref is None`` is the COMMON case and a first-class success:
    most faces in most photos belong to people who are not enrolled. It is not
    an error, not a warning, and not a reason to fail the run.
    """

    face_index: int
    bbox: BoundingBox
    detect_confidence: float
    resolved_user_ref: UserRef | None
    match_score: float | None

    def __post_init__(self) -> None:
        # Mirrors the database CHECK. Failing here as well means a bug is
        # caught at construction rather than at the INSERT, where the traceback
        # says far less about where the wrong pair came from.
        if (self.resolved_user_ref is None) != (self.match_score is None):
            raise ValueError(
                "resolved_user_ref and match_score must be set together or not at all"
            )


@dataclass(frozen=True, slots=True)
class RegisteredSeed:
    user_ref: UserRef
    seed_id: UUID


@dataclass(frozen=True, slots=True)
class AttributionOutcome:
    run_id: UUID
    faces: tuple[AttributedFace, ...]
    seeds: tuple[RegisteredSeed, ...]


class AttributionUnavailable(RuntimeError):
    """A face-provider call failed. The route maps this to 503: the run is
    marked failed, nothing partial is written, and the proxy may retry."""
