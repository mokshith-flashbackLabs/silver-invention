"""Phone- and email-shaped runs in intel text (spec §6.3).

Excerpts holding one are DROPPED (redacting would break the verbatim property,
#49). Model-written and page-derived free text is MASKED. The phone rule is the
log redactor's, with its ISO-date and UUID carve-outs, so a date is never taken for
a phone number."""

from __future__ import annotations

import re

from imageshield.redaction import PHONE_REDACTED, redact_string

MASK = "«masked»"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def contains_pii(text: str) -> bool:
    return redact_string(text) != text or _EMAIL_RE.search(text) is not None


def mask(text: str) -> tuple[str, int]:
    phones_masked = redact_string(text)
    count = phones_masked.count(PHONE_REDACTED)
    masked, emails = _EMAIL_RE.subn(MASK, phones_masked.replace(PHONE_REDACTED, MASK))
    return masked, count + emails
