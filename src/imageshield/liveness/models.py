"""Domain types shared by the liveness store, provider, uploader and routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

# Mirrors the liveness_status Postgres enum (migrations/0001). 'pending' and
# 'consumed' are carried for schema parity; step 3 never writes them.
LivenessStatus = Literal["created", "pending", "passed", "failed", "expired", "consumed"]

ProviderStatus = Literal["created", "in_progress", "succeeded", "failed", "expired"]


@dataclass(frozen=True, slots=True)
class LivenessSessionRow:
    """One row of ``liveness_sessions``, as the store returns it."""

    session_id: UUID
    user_ref: UUID
    provider_session_id: str
    status: str
    confidence: float | None
    failure_reason: str | None
    attempt_number: int
    reference_image_uri: str | None
    audit_image_uris: tuple[str, ...] | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime
    consumed_at: datetime | None
    result_idempotency_key: str | None


class CreateRejection(Enum):
    """Why a session create was refused. Maps to 409 / 429 at the route."""

    PASSED_UNCONSUMED = "passed_unconsumed"
    ATTEMPTS_EXCEEDED = "attempts_exceeded"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What ``GetFaceLivenessSessionResults`` reported, provider-neutrally.

    ``reference_image``/``audit_images`` are bytes held in memory only — they
    are PUT through presigned URLs and discarded, never persisted by us
    (CLAUDE.md §3.3, INVARIANTS.md #9).
    """

    status: ProviderStatus
    confidence: float | None
    reference_image: bytes | None
    audit_images: tuple[bytes, ...]


class UploadError(RuntimeError):
    """A presigned PUT failed. Message must never include the URL — the query
    string carries the signature, which is a credential."""


class ProviderSessionNotFound(RuntimeError):
    """The provider no longer knows this session (its own TTL elapsed)."""
