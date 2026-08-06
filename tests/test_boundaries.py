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


def _source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "src/ scan found nothing — path wrong?"
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
