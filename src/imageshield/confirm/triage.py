"""Pure triage: severity classification, the CSAM tripwire, and dedup.

No I/O, no boto3 — every function here takes plain values and returns a plain
value, so the decision table is exhaustively testable without a fake client
or a database. The confirm worker (Task 9) is the only caller and it owns all
the I/O: fetching bytes, calling :mod:`imageshield.confirm.moderation`, and
persisting the result.

``classify`` never returns ``"unassessed"`` — that severity exists in the
0021 CHECK constraint for hits the worker could not fetch or assess at all,
and only the worker assigns it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from imageshield.confirm.moderation import ModerationLabel
from imageshield.confirm.phash import hamming

Severity = Literal[
    "ncii_suspected",
    "explicit_unmatched",
    "unassessed",
    "benign_copy",
    "likely_not_subject",
]

# Worst (most urgent for a human reviewer) to least. "unassessed" sits between
# the two explicit bands and the two non-explicit ones: it means "we don't
# know", which is worse than a confirmed benign match but better than not
# looking at all a confirmed-explicit hit would be.
SEVERITY_RANK: dict[str, int] = {
    "ncii_suspected": 0,
    "explicit_unmatched": 1,
    "unassessed": 2,
    "benign_copy": 3,
    "likely_not_subject": 4,
}

# Rekognition's moderation taxonomy nests specific labels ("Exposed
# Genitalia") under a parent ("Explicit Nudity"); some responses report the
# parent itself as a top-level label with no parent of its own, hence the
# `parent_name` check also matching `name`.
EXPLICIT_LABEL_PARENTS = frozenset({"Explicit Nudity", "Explicit"})
EXPLICIT_MIN_CONFIDENCE = 80.0


def is_explicit(labels: Sequence[ModerationLabel]) -> bool:
    return any(
        (label.name in EXPLICIT_LABEL_PARENTS or label.parent_name in EXPLICIT_LABEL_PARENTS)
        and label.confidence >= EXPLICIT_MIN_CONFIDENCE
        for label in labels
    )


def classify(
    *, explicit: bool, face_match_score: float | None, face_match_threshold: float
) -> Severity:
    matched = face_match_score is not None and face_match_score >= face_match_threshold
    if explicit and matched:
        return "ncii_suspected"
    if explicit:
        return "explicit_unmatched"
    if matched:
        return "benign_copy"
    return "likely_not_subject"


def csam_quarantine(*, explicit: bool, min_age_low: float | None, age_low_threshold: int) -> bool:
    return explicit and min_age_low is not None and min_age_low < age_low_threshold


def find_duplicate(
    new_phash: int, decided: Sequence[tuple[UUID, int]], hamming_max: int
) -> UUID | None:
    best_id: UUID | None = None
    best_distance: int | None = None
    for candidate_id, candidate_phash in decided:
        distance = hamming(new_phash, candidate_phash)
        if distance <= hamming_max and (best_distance is None or distance < best_distance):
            best_id = candidate_id
            best_distance = distance
    return best_id
