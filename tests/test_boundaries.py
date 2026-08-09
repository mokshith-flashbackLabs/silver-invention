"""Tripwires for the identity boundary (CLAUDE.md §4 #1, step-4 done-when).

Step 9 adds these as CI greps; having them as tests too means a violation
fails BEFORE a PR exists. Permanent — never delete.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# The fragmentation bug's fingerprints: any face-search call in this codebase
# means identity is about to come from a similarity score (INVARIANTS #1).
FORBIDDEN_SEARCH = re.compile(
    r"search_faces_by_image|SearchFacesByImage|search_users_by_image|search_faces\b|SearchFaces\b"
)

# No S3 client, ever (CLAUDE.md §3.3): presigned URLs via httpx are the only
# path to bytes.
FORBIDDEN_S3 = re.compile(r"""boto3\.client\(\s*["']s3["']|boto3\.resource\(\s*["']s3["']""")

# CLAUDE.md §7.2 / INVARIANTS #15c. Provider A's 0.92 and Provider B's 0.92 are
# different quantities with different distributions. Combining them yields a
# number with no meaning that will look entirely plausible — and calibration is
# exactly where the temptation lives, because "just normalise onto a common
# scale" is the obvious-looking move. NEAR-TERM-BUILD.md §2.3 specified that
# very thing (a calibrate(rawScore) adapter method and a calibrated_score
# column) until step 7 corrected it.
#
# Deliberately lexical, and deliberately covering comments and docstrings as
# well as code: to a grep, a module explaining at length why it does not
# average scores is indistinguishable from one that does. There is NO
# allowlist. If a legitimate use ever needs to exist here, adding it should
# cost a code review — which is the entire point.
# NOTE ON THE PATTERN: a plain \bmean\b|\baverage\b|\bavg\b does NOT catch
# `avg_score`, because `_` is a word character so \b never matches between
# them. That is the single most likely shape a real violation would take, and
# an earlier draft of this test was blind to it — verified by inserting
# `avg_score = sum(scores) / len(scores)` into bands.py and watching the test
# still pass. Hence the four alternatives: bare word, identifier prefix,
# identifier suffix, camelCase.
#
# English derivatives stay out on purpose: "meaning", "meaningful",
# "meaningless" and "averaged" all appear in prose explaining what this code
# does NOT do, and flagging them would push authors toward deleting the
# explanation rather than the arithmetic.
_MATHS_TOKEN = r"(?:mean|average|avg)"
FORBIDDEN_CROSS_PROVIDER_MATHS = re.compile(
    rf"(?i:\b{_MATHS_TOKEN}\b)"        # bare word:         "average them"
    rf"|(?i:\b{_MATHS_TOKEN})_"        # identifier prefix: avg_score
    rf"|_(?i:{_MATHS_TOKEN}\b)"        # identifier suffix: score_avg
    rf"|(?i:\b{_MATHS_TOKEN})(?=[A-Z])"  # camelCase:       avgScore
)

# Only the two directories that hold scores. The schema lint elsewhere in src/
# legitimately says "the two words that mean 'we kept pixels around'".
SCORE_DIRS = ("search", "calibration")


def _source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "src/ scan found nothing — path wrong?"
    return files


def _scored_source_files() -> list[Path]:
    files = [p for d in SCORE_DIRS for p in sorted((SRC / "imageshield" / d).rglob("*.py"))]
    assert files, "search/ and calibration/ scan found nothing — paths wrong?"
    return files


def test_no_face_search_anywhere_in_src() -> None:
    offenders = [
        str(path)
        for path in _source_files()
        if FORBIDDEN_SEARCH.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_s3_client_anywhere_in_src() -> None:
    offenders = [
        str(path)
        for path in _source_files()
        if FORBIDDEN_S3.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_cross_provider_averaging_in_scoring_code() -> None:
    """Permanent. Verified to actually fire by inserting a violation into
    bands.py, watching this fail, and removing it — a tripwire nobody has seen
    trip is not known to work."""
    offenders = [
        f"{path}:{i}: {line.strip()}"
        for path in _scored_source_files()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN_CROSS_PROVIDER_MATHS.search(line)
    ]
    assert offenders == []
