"""One normalisation for hashing AND verification (spec §4.3). If the two ever
normalised differently, a verbatim quote could fail and a changed page could hash
equal."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text).replace("\u00a0", " ")).strip()


def content_sha256(normalised: str) -> str:
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
