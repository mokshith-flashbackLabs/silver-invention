"""INVARIANTS #49: every citation is a verbatim substring of text WE fetched."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from imageshield.intel.bounds import MAX_QUOTE_CHARS, MIN_QUOTE_CHARS
from imageshield.intel.pii import contains_pii
from imageshield.intel.text import content_sha256, normalise

DropReason = Literal["not_a_substring", "too_short", "too_long", "pii_in_excerpt"]


@dataclass(frozen=True)
class VerifiedQuote:
    text: str
    char_start: int
    char_end: int
    sha256: str


def verify_quote(document: str, quote: str) -> VerifiedQuote | DropReason:
    """``document`` is already normalised. The quote is normalised the same way."""
    candidate = normalise(quote)
    if len(candidate) < MIN_QUOTE_CHARS:
        return "too_short"
    if len(candidate) > MAX_QUOTE_CHARS:
        return "too_long"
    if contains_pii(candidate):
        return "pii_in_excerpt"
    start = document.find(candidate)
    if start < 0:
        return "not_a_substring"
    return VerifiedQuote(candidate, start, start + len(candidate), content_sha256(candidate))
