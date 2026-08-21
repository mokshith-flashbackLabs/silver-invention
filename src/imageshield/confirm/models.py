"""Shared constants and payload shapes for the confirm pipeline (Task 4).

CLAUDE.md §10: messages carry IDs, never payloads. :class:`ConfirmContext` is
the shape the ``confirm:hits`` worker (specified, not yet built) re-derives
from Postgres for one hit — never trusted off the queue message itself; the
message on the wire is an :class:`imageshield.outbox.OutboxPayload`
(``event`` + ``id``), and the worker re-reads the authoritative row to build
one of these.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId, UserRef

# Emitted onto imageshield.outbox.QUEUE_CONFIRM_HITS for every
# still-unconfirmed hit a completed run touched (spec 2026-08-21 §1 — the
# gate is deliberately wide; the provider row is the spend control).
CONFIRM_REQUESTED_EVENT = "confirm.hit_requested"

# Registered as a providers row in migration 0021 so the existing
# budget/breaker/spend machinery governs the confirm pass (INVARIANTS #37-41).
REKOGNITION_CONFIRM_ID = ProviderId("rekognition_confirm")


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
