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

# ── the one sanctioned machine confirm (2026-09-14) ───────────────────────
#
# Spec docs/superpowers/specs/2026-09-14-auto-confirm-and-reviewer-feed-design.md;
# INVARIANTS #19/#47 as amended the same day.

# The ONLY non-human value `infringements.confirm_decided_by` may ever carry.
# Every other confirmed row names a person: the `operator` string the review
# console authenticated, or the constant 'subject'. Written only by
# `confirm/store.py::record_auto_confirmed`, and read by `score/store.py` to
# tell "nobody has answered yet" apart from "nobody will ever be asked".
AUTO_CONFIRM_DECIDED_BY = "auto:nsfw"

# The ONE severity the machine may confirm on its own: explicit content AND a
# face match at or above `confirm_face_match_threshold` (owner decision D3).
# `explicit_unmatched` -- explicit, but the face match FAILED -- deliberately
# stays a human decision, because a failed face match is the strongest
# false-positive signal this pipeline has. The other four severities are
# unchanged: they order the review queue and decide nothing.
AUTO_CONFIRM_SEVERITY = "ncii_suspected"


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
