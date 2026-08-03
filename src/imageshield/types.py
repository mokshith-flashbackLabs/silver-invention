"""Typed identifiers (CLAUDE.md §10).

``NewType`` gives static discipline — a bare ``str`` (or bare ``UUID``) is a
mypy error where a ``UserRef`` is expected, so identifiers can't silently swap.
The runtime enforcement that actually matters lives in the pydantic request
models at the HTTP boundary; the ``parse_*`` constructors here are for every
other entry point (queue payloads, DB rows, CLI args).

mypy/pyright strict runs as a blocking check — NewType is worthless without it.
"""

from __future__ import annotations

import re
from typing import NewType
from uuid import UUID

UserRef = NewType("UserRef", UUID)
SessionId = NewType("SessionId", UUID)
ProviderId = NewType("ProviderId", str)
UrlHash = NewType("UrlHash", str)

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_URL_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


# The rejected value is never echoed in error messages: these parsers sit on
# request/queue boundaries and the invalid input could be anything — including
# a phone number, which must never reach a log line (CLAUDE.md §3.2).
def _parse_uuid(value: str | UUID, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value.strip())
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"{label} must be a UUID (received a non-UUID value of length {len(str(value))})"
        ) from None


def parse_user_ref(value: str | UUID) -> UserRef:
    return UserRef(_parse_uuid(value, "user_ref"))


def parse_session_id(value: str | UUID) -> SessionId:
    return SessionId(_parse_uuid(value, "session_id"))


def parse_provider_id(value: str) -> ProviderId:
    if not _PROVIDER_ID_RE.fullmatch(value):
        raise ValueError("provider_id must match ^[a-z][a-z0-9_-]{1,63}$ (lowercase slug)")
    return ProviderId(value)


def parse_url_hash(value: str) -> UrlHash:
    normalised = value.lower()
    if not _URL_HASH_RE.fullmatch(normalised):
        raise ValueError(
            "url_hash must be 64 lowercase hex characters (sha256 of the normalised URL); "
            f"received length {len(value)}"
        )
    return UrlHash(normalised)
