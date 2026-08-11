"""Tripwires for the identity boundary (CLAUDE.md §4 #1, step-4 done-when).

Step 9 adds these as CI greps; having them as tests too means a violation
fails BEFORE a PR exists. Permanent — never delete.
"""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# The fragmentation bug's fingerprints (INVARIANTS #1). The pattern is
# unchanged from when this gate covered all of src/; what changed is WHERE it is
# applied — see ENROLMENT_PATH and ATTRIBUTION_DIR below.
FORBIDDEN_SEARCH = re.compile(
    r"search_faces_by_image|SearchFacesByImage|search_users_by_image|search_faces\b|SearchFaces\b"
)

# Where identity is established. A face-search call HERE is the fragmentation
# bug itself: the old system's processFaceRecognition (server.js:9585) searched
# the collection during enrolment and minted or overwrote a user_ref from the
# result. INVARIANTS #1 has always been worded against this path specifically
# ("SearchFacesByImage must not appear in the enrolment path"); until now the
# test was stricter than the rule and covered all of src/.
ENROLMENT_PATH = (
    "liveness",
    "enrolment",
    "subjects",
)
ENROLMENT_PATH_FILES = (
    "http/routes/liveness.py",
    "http/routes/enrolments.py",
)

# The ONE exemption (INVARIANTS #1a). Attribution matches a face in a
# THIRD-PARTY photo against a caller-supplied list of already-enrolled
# user_refs. It cannot corrupt an identity: no user_ref is created, reassigned
# or merged, every candidate was named in the request, and a non-match is a
# first-class success whose worst case is a seed not registered.
#
# Exactly one directory. Adding a second costs a code change and a review, which
# is the whole point of naming it here rather than passing an exclude list.
ATTRIBUTION_DIR = "attribution"

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

# Only the directories that hold scores. The schema lint elsewhere in src/
# legitimately says "the two words that mean 'we kept pixels around'".
SCORE_DIRS = ("search", "calibration")

# devtools/calibrate/ is scanned too: it is the harness that produces the
# numbers a human reads before trusting a provider, so "just average the
# providers" is at least as likely to be written there as in bands.py.
# devtools/harness/web/dist/ is vendored JS and is NOT scanned.
CALIBRATE_CLI = Path(__file__).resolve().parents[1] / "devtools" / "calibrate"


def _source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "src/ scan found nothing — path wrong?"
    return files


def _enrolment_path_files() -> list[Path]:
    """Every module where identity is established."""
    package = SRC / "imageshield"
    files = [p for d in ENROLMENT_PATH for p in sorted((package / d).rglob("*.py"))]
    files += [package / name for name in ENROLMENT_PATH_FILES]
    missing = [p for p in files if not p.exists()]
    assert not missing, f"enrolment-path scan lists files that do not exist: {missing}"
    return files


def _files_outside_attribution() -> list[Path]:
    """All of src/ except the one exempt directory."""
    exempt = SRC / "imageshield" / ATTRIBUTION_DIR
    files = [p for p in sorted(SRC.rglob("*.py")) if exempt not in p.parents]
    assert files, "src/ scan found nothing — path wrong?"
    return files


def _scored_source_files() -> list[Path]:
    files = [p for d in SCORE_DIRS for p in sorted((SRC / "imageshield" / d).rglob("*.py"))]
    files += sorted(CALIBRATE_CLI.rglob("*.py"))
    assert files, "search/, calibration/ and devtools/calibrate/ scan found nothing?"
    return files


# INVARIANTS #8 / #1b, tightened by step 8. Both age floors come from config:
# MIN_ENROLMENT_AGE (13) and MIN_DISCOVERY_AGE (18). An inline 18 is the exact
# shape the v2 change would silently miss — the config value lowered while some
# literal still gates on the old one, failing in the direction that scans a
# minor.
#
# Two deliberate narrowings of the step-8 brief's `grep -rn "\b18\b" src/`:
#
# 1. Only the directories where an age could plausibly be compared.
#    calibration/report.py uses 18 as a column width in format specifiers and
#    redaction.py uses it as a phone-length bound. Neither is an age, and
#    rewriting them to satisfy a grep would be the grep changing the code.
#
# 2. **Executable code only** — comments and string literals are stripped with
#    `tokenize` before matching. This is the opposite choice from
#    FORBIDDEN_CROSS_PROVIDER_MATHS above, and for a reason: there, the risk was
#    arithmetic, which a comment about averaging is indistinguishable from to a
#    grep. Here the risk is a *comparison*, which cannot live in a comment, while
#    the prose explaining why v1 ships at 18 is exactly what a future reader
#    needs. Flagging it would push authors toward deleting the explanation.
AGE_TOKEN = re.compile(r"\b18\b")
AGE_SCAN_DIRS = ("subjects", "http")


def _age_scannable_files() -> list[Path]:
    files = [p for d in AGE_SCAN_DIRS for p in sorted((SRC / "imageshield" / d).rglob("*.py"))]
    files.append(SRC / "imageshield" / "config.py")
    assert files, "subjects/, http/ and config.py scan found nothing?"
    return files


def _code_tokens(path: Path) -> list[tuple[int, str]]:
    """(line number, token text) for everything that is not a comment or a
    string literal — i.e. the part of the file that can actually compare an age.
    """
    with path.open("rb") as handle:
        return [
            (token.start[0], token.string)
            for token in tokenize.tokenize(handle.readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        ]


def test_no_inline_age_literal_where_ages_are_decided() -> None:
    """Permanent. Both ages are read from config at request time (INVARIANTS #8),
    so v2 lowering MIN_DISCOVERY_AGE is a config change plus the minor-handling
    code — never a hunt for literals.

    Verified to actually fire: inserting `if age >= 18:` into
    subjects/eligibility.py makes this fail. A tripwire nobody has seen trip is
    not known to work.
    """
    offenders = [
        f"{path}:{line}: {text}"
        for path in _age_scannable_files()
        for line, text in _code_tokens(path)
        if AGE_TOKEN.search(text)
    ]
    assert offenders == []


def test_no_face_search_in_the_enrolment_path() -> None:
    """PERMANENT. A hit here IS the fragmentation bug (INVARIANTS #1).

    Identity comes from the authenticated request, always. A face search in
    this path means a similarity score is about to decide who someone is —
    which in the old system minted a fresh user_ref for a returning user
    scoring 92 against a 95 threshold, orphaning their monitoring history and
    leaving their old faces in the collection under a dead ID.
    """
    offenders = [
        str(path)
        for path in _enrolment_path_files()
        if FORBIDDEN_SEARCH.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_face_search_appears_only_in_the_attribution_module() -> None:
    """PERMANENT. The exemption in INVARIANTS #1a is exactly one directory.

    Narrower than the enrolment-path gate above and less catastrophic when it
    fires: a face search in, say, ``search/`` or ``recheck/`` is not the
    fragmentation bug, but it is face search somewhere nobody reasoned about.
    Both tests exist because the two failures deserve different alarm.
    """
    offenders = [
        str(path)
        for path in _files_outside_attribution()
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
