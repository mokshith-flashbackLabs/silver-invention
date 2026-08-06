"""Domain types for enrolment (CLAUDE.md §8 step 4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

# Stored in liveness_sessions.failure_reason when IndexFaces' HIGH quality
# filter rejects the frame, and surfaced verbatim as the response `reason` so
# the proxy can tell "liveness passed, enrolment didn't" apart and start a
# fresh session (step-4 brief).
QUALITY_REJECTED_REASON = "quality_rejected"


@dataclass(frozen=True, slots=True)
class NewEnrolment:
    """What the route hands the store after a successful IndexFaces."""

    user_ref: UUID
    collection_id: str
    external_face_id: str
    quality_score: float | None
    model_id: str
    source_object_uri: str


@dataclass(frozen=True, slots=True)
class EnrolmentRow:
    """One row of ``enrolments``, as the store returns it."""

    enrolment_id: UUID
    session_id: UUID
    user_ref: UUID
    collection_id: str
    external_face_id: str
    quality_score: float | None
    model_id: str
    source_object_uri: str
    status: str
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class IndexedFace:
    """IndexFaces accepted the frame: the FaceId now in the collection, the
    detection confidence (quality_score), and the face model version that
    produced the vector (INVARIANTS #4 — every vector-bearing row carries
    model_id)."""

    face_id: str
    quality_score: float | None
    model_id: str


@dataclass(frozen=True, slots=True)
class IndexRejected:
    """The HIGH quality filter rejected the frame. ``reasons`` comes from
    ``UnindexedFaces[].Reasons`` (or NO_FACE_DETECTED when both lists were
    empty) — logged for ops, never stored per-reason."""

    reasons: tuple[str, ...]


class FaceIndexUnavailable(RuntimeError):
    """A Rekognition face-index call failed. The route maps this to 503:
    nothing was written, the session is not consumed, and the proxy retries
    the whole result call with the same Idempotency-Key."""
