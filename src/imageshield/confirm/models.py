"""Shared constants and payload shapes for the confirm pipeline (Task 4).

CLAUDE.md §10: messages carry IDs, never payloads. :class:`ConfirmContext` is
the shape the ``confirm:hits`` worker (specified, not yet built) re-derives
from Postgres for one hit — never trusted off the queue message itself; the
message on the wire is an :class:`imageshield.outbox.OutboxPayload`
(``event`` + ``id``), and the worker re-reads the authoritative row to build
one of these.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId, UserRef

# Emitted onto imageshield.outbox.QUEUE_CONFIRM_HITS when a review-band
# infringement meets its provider's "most similar" criteria (design doc §7).
CONFIRM_REQUESTED_EVENT = "confirm.hit_requested"

# Registered as a providers row in migration 0021 so the existing
# budget/breaker/spend machinery governs the confirm pass (INVARIANTS #37-41).
REKOGNITION_CONFIRM_ID = ProviderId("rekognition_confirm")


class ConfirmCriteria(BaseModel):
    """Per-provider "most similar" thresholds that decide whether a hit gets
    enqueued for confirmation (design doc §7: ``CONFIRM_HIVE_MIN_SCORE`` /
    ``CONFIRM_GOOGLE_KINDS``, read from config by the caller — this model
    just carries the resolved values)."""

    model_config = ConfigDict(frozen=True)

    hive_min_score: Decimal
    google_kinds: frozenset[str]


class ConfirmContext(BaseModel):
    """Everything the confirm worker needs about one hit, re-read from
    Postgres rather than trusted off the queue message (CLAUDE.md §10)."""

    model_config = ConfigDict(frozen=True)

    infringement_id: UUID
    user_ref: UserRef
    confirm_state: str
    image_url: str | None
    page_url: str
    run_id: UUID | None  # representative attestation's last_run_id
