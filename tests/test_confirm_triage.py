"""Pure triage classifier: severity decision table, CSAM tripwire, dedup.

No I/O, no boto3 — every case here is exhaustive and deterministic (task-7
brief). ``classify`` never returns ``unassessed``; that severity is assigned
by the worker (Task 9) for hits it could not fetch at all.
"""

from __future__ import annotations

from uuid import uuid4

from imageshield.confirm.moderation import ModerationLabel
from imageshield.confirm.triage import (
    SEVERITY_RANK,
    classify,
    csam_quarantine,
    find_duplicate,
    is_explicit,
)


def test_explicit_and_matched_is_ncii() -> None:
    result = classify(explicit=True, face_match_score=95.0, face_match_threshold=92.0)
    assert result == "ncii_suspected"


def test_explicit_unmatched_is_still_reviewed_high() -> None:
    unscored = classify(explicit=True, face_match_score=None, face_match_threshold=92.0)
    below_threshold = classify(explicit=True, face_match_score=70.0, face_match_threshold=92.0)
    assert unscored == "explicit_unmatched"
    assert below_threshold == "explicit_unmatched"


def test_benign_copy_when_face_matches_without_nudity() -> None:
    result = classify(explicit=False, face_match_score=93.0, face_match_threshold=92.0)
    assert result == "benign_copy"


def test_likely_not_subject_when_nothing_matches() -> None:
    result = classify(explicit=False, face_match_score=None, face_match_threshold=92.0)
    assert result == "likely_not_subject"


def test_is_explicit_needs_parent_and_confidence() -> None:
    strong = ModerationLabel(
        name="Exposed Genitalia", parent_name="Explicit Nudity", confidence=91.0
    )
    weak = ModerationLabel(
        name="Exposed Genitalia", parent_name="Explicit Nudity", confidence=61.0
    )
    unrelated = ModerationLabel(name="Violence", parent_name="Violence", confidence=99.0)
    top_level = ModerationLabel(name="Explicit Nudity", parent_name="", confidence=95.0)
    assert is_explicit([strong]) and is_explicit([top_level])
    assert not is_explicit([weak]) and not is_explicit([unrelated]) and not is_explicit([])


def test_csam_tripwire_requires_both_signals() -> None:
    assert csam_quarantine(explicit=True, min_age_low=12.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=False, min_age_low=12.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=True, min_age_low=25.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=True, min_age_low=None, age_low_threshold=18)


def test_find_duplicate_honours_hamming_and_order() -> None:
    a, b = uuid4(), uuid4()
    decided = [(a, 0), (b, 1)]
    assert find_duplicate(0, decided, hamming_max=8) == a  # exact beats near
    assert find_duplicate(0b11111, [(b, 0)], hamming_max=4) is None


def test_severity_rank_total_order() -> None:
    assert [k for k, _ in sorted(SEVERITY_RANK.items(), key=lambda kv: kv[1])] == [
        "ncii_suspected",
        "explicit_unmatched",
        "unassessed",
        "benign_copy",
        "likely_not_subject",
    ]
