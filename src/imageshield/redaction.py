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

# ISO dates / timestamps ("2026-08-03", "2026-08-03 12:47:01") are the one
# legitimate log shape that collides with the candidate pattern.
_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")

PHONE_REDACTED = "[REDACTED:phone-shaped]"


def redact_string(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        text = match.group(0)
        if _ISO_DATE_PREFIX_RE.match(text):
            return text
        digit_count = sum(ch.isdigit() for ch in text)
        return PHONE_REDACTED if 7 <= digit_count <= 15 else text

    return _CANDIDATE_RE.sub(_replace, value)


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
