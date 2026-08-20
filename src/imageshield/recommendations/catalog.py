"""The recommendation rule table — pure, no I/O.

Design: ``docs/superpowers/specs/2026-08-19-protection-score-design.md`` §5.
:func:`desired` is the entire rule table: given a person's current
:class:`~imageshield.score.engine.ScoreState` and the active threat events
needing a priority scan, it returns exactly the recommendations that *should*
be open right now, in a fixed order.

Reconciling that against what is actually open — inserting new ones,
completing ones no longer desired, expiring ones past ``expires_at``, and
honouring a ``dismissed`` row forever — is Task 12's job in the store (stated
in the brief because both layers' tests assert the contract, but the sync
logic itself lives there, not here). This module never touches a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.score.engine import ScoreState, ScoreWeights

RecKind = Literal[
    "complete_enrolment",
    "add_seed_photos",
    "refresh_seeds",
    "respond_to_hits",
    "run_priority_scan",
]


class RecSpec(BaseModel):
    """One recommendation the catalog says should be open. ``source_event_id``
    is set only for ``run_priority_scan`` — every other kind ties to nothing
    but the person's own state, which is also why they carry no
    ``expires_at``: a threat event has a lifetime, a stale seed portfolio
    does not."""

    model_config = ConfigDict(frozen=True)

    kind: RecKind
    params: dict[str, Any]
    source_event_id: UUID | None = None
    expires_at: datetime | None = None


class EventNeedingScan(BaseModel):
    """An active threat event matched to this person, past its ``starts_at``,
    with no completed scan run yet counted against it. The store resolves
    this list; the catalog only turns each one into a recommendation."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    expires_at: datetime


def desired(
    state: ScoreState,
    events_needing_scan: Sequence[EventNeedingScan],
    w: ScoreWeights,
) -> tuple[RecSpec, ...]:
    specs: list[RecSpec] = []

    if not state.enrolment_active:
        specs.append(RecSpec(kind="complete_enrolment", params={}))

    if state.seed_count < w.seed_target:
        specs.append(
            RecSpec(
                kind="add_seed_photos",
                params={"target": w.seed_target, "have": state.seed_count},
            )
        )

    if state.seed_count > 0 and not state.seeds_fresh:
        specs.append(
            RecSpec(kind="refresh_seeds", params={"fresh_days": w.seed_fresh_days})
        )

    if state.awaiting_feedback_count > 0:
        specs.append(
            RecSpec(
                kind="respond_to_hits",
                params={"count": state.awaiting_feedback_count},
            )
        )

    for event in events_needing_scan:
        specs.append(
            RecSpec(
                kind="run_priority_scan",
                params={"event_id": str(event.event_id)},
                source_event_id=event.event_id,
                expires_at=event.expires_at,
            )
        )

    return tuple(specs)


__all__ = ["EventNeedingScan", "RecKind", "RecSpec", "desired"]
