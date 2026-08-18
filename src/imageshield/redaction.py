"""Phone-number tripwire (build spec Phase 1 §6, CLAUDE.md §3.2).

This service must never hold a phone number, so any phone-shaped string in a
log line is evidence of a boundary breach. The structlog processor here
redacts rather than passes through: over-redaction is acceptable, leaking a
number is not.
"""

from __future__ import annotations

import re

from structlog.types import EventDict, WrappedLogger

# Candidate: an optional +, then digits and common phone separators, starting
# and ending on a digit. Confirmed as phone-shaped when the digit count lands
# in the 7-15 range (ITU E.164 bounds).
_CANDIDATE_RE = re.compile(r"\+?\d[\d\-\s().]{4,18}\d")

# A complete ISO-8601 date/datetime is the one legitimate log shape that
# collides with the candidate pattern above -- and it collides on more than
# just the date. Fractional seconds ("23.456789") are 1-8 digits joined by a
# '.', which the candidate character class already allows, so on their own
# they parse as a phone-shaped run and land in the 7-15 "redact" band —
# exactly what turned "2026-08-17T17:57:23.456789Z" into
# "2026-08-17T17:57:[REDACTED:phone-shaped]Z" in real logs. A prefix-only
# guard (the previous approach) protects "2026-08-17" and leaves the time
# portion for the candidate regex to eat.
#
# This pattern instead matches the WHOLE timestamp -- date, T-or-space
# separator, time, optional fractional seconds, optional Z / ±HH:MM / ±HHMM
# offset -- so it is found and protected as one span BEFORE the candidate
# regex ever runs over that span, wherever it appears in the string (not only
# at position 0: timestamps show up inside longer messages, e.g. a
# structlog-rendered `"timestamp": "..."` field embedded in a JSON line).
_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # date
    r"(?:"
    r"[T ]\d{2}:\d{2}:\d{2}"  # time, only recognised paired with a date
    r"(?:\.\d+)?"  # optional fractional seconds
    r"(?:Z|[+-]\d{2}:?\d{2})?"  # optional Z or UTC offset, colon optional
    r")?"
)

# The other legitimate log shape that collides with the candidate pattern: a
# canonical UUID (request_id, user_ref, ...) is mostly digits and hyphens --
# hex letters a-f are only 6 of 16 possible characters per position, so plenty
# of real UUIDs have whole runs of nothing but digits, which land squarely in
# the 7-15 "redact" band. That is exactly what turned
# "5f8e2c03-1f2b-4c3d-9e0f-a1b2c3d4e5f6" into
# "5f8e2c03-1f[REDACTED:phone-shaped]f[REDACTED:phone-shaped]f1f" in real
# deployed logs, and request_id is the one field that correlates a log line
# back to a request.
#
# Deliberately narrow, mirroring the ISO carve-out above: only the canonical
# hyphenated 8-4-4-4-12 hex form is protected, matched wherever it appears in
# the string. The group lengths are exact and not loosened -- a bare 32-hex
# string with no hyphens gets NO protection here (see redact_string's
# docstring), because loosening them is exactly how a phone number could hide
# by being formatted to look UUID-ish.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# The two known-safe shapes, tried at every position before the candidate scan
# runs -- see redact_string. They never overlap: a canonical UUID's hyphens
# are 5 apart (after its 8-, 4- and 4-hex groups) and an ISO datetime's are 3
# apart (after the 4-digit year and 2-digit month), so no span of text can
# satisfy both alternatives starting at the same offset.
_PROTECTED_RE = re.compile(rf"(?:{_UUID_RE.pattern})|(?:{_ISO_DATETIME_RE.pattern})")

PHONE_REDACTED = "[REDACTED:phone-shaped]"


def _redact_candidates(text: str) -> str:
    """Phone-shaped-run scan with NO awareness of timestamps or UUIDs.
    Callers must only ever pass this a slice that has already had complete
    timestamps and UUIDs carved out of it — see ``redact_string``."""

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digit_count = sum(ch.isdigit() for ch in candidate)
        return PHONE_REDACTED if 7 <= digit_count <= 15 else candidate

    return _CANDIDATE_RE.sub(_replace, text)


def redact_string(value: str) -> str:
    """Preserve every complete ISO-8601 date/datetime span and every canonical
    UUID span verbatim, then run the phone-shaped candidate scan over
    everything in between and around them.

    DELIBERATE OVER-REDACTION, DO NOT "FIX": a bare 7-15 digit run that is NOT
    part of a recognised ISO datetime or canonical UUID -- e.g. an AWS account
    id such as '225989356895', or that same UUID with its hyphens stripped --
    is still redacted by ``_redact_candidates`` below. It is shape-identical
    to a real phone number (same digit count, ITU E.164 is 7-15 digits) and
    this module's whole premise is that over-redaction is acceptable and
    leaking a real phone number is not. Do not add key-based or shape-based
    allowlisting to narrow this.
    """
    pieces: list[str] = []
    cursor = 0
    for match in _PROTECTED_RE.finditer(value):
        pieces.append(_redact_candidates(value[cursor : match.start()]))
        pieces.append(match.group(0))  # the protected span itself, untouched
        cursor = match.end()
    pieces.append(_redact_candidates(value[cursor:]))
    return "".join(pieces)


def redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {key: redact_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def redact_phone_shapes(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: deep-redact every value, including ``event`` itself."""
    return {key: redact_value(val) for key, val in event_dict.items()}
