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

PHONE_REDACTED = "[REDACTED:phone-shaped]"


def _redact_candidates(text: str) -> str:
    """Phone-shaped-run scan with NO ISO-timestamp awareness. Callers must
    only ever pass this a slice that has already had complete timestamps
    carved out of it — see ``redact_string``."""

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digit_count = sum(ch.isdigit() for ch in candidate)
        return PHONE_REDACTED if 7 <= digit_count <= 15 else candidate

    return _CANDIDATE_RE.sub(_replace, text)


def redact_string(value: str) -> str:
    """Preserve every complete ISO-8601 date/datetime span verbatim, then run
    the phone-shaped candidate scan over everything in between and around
    them.

    DELIBERATE OVER-REDACTION, DO NOT "FIX": a bare 7-15 digit run that is NOT
    part of a recognised ISO datetime -- e.g. an AWS account id such as
    '225989356895' -- is still redacted by ``_redact_candidates`` below. It is
    shape-identical to a real phone number (same digit count, ITU E.164 is
    7-15 digits) and this module's whole premise is that over-redaction is
    acceptable and leaking a real phone number is not. Do not add key-based or
    shape-based allowlisting to narrow this.
    """
    pieces: list[str] = []
    cursor = 0
    for match in _ISO_DATETIME_RE.finditer(value):
        pieces.append(_redact_candidates(value[cursor : match.start()]))
        pieces.append(match.group(0))  # the timestamp itself, untouched
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
