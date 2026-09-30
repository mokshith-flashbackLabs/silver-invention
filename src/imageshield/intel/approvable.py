"""The approvability predicate (spec §3.6). ``why_not`` answers every refusal knowable at
read time, in a fixed order: not_decidable, evidence_retracted, uncorroborated, tags_unmapped.

Both reads call it (through ``read_flags``) and so does the decision, inside its transaction.
So a proposal the panel shows as approvable is never one the decision refuses with a 409,
except for races the transaction itself catches (proposal_not_pending,
proposal_cell_awaiting_publish).

Which kinds are decidable depends on the build step (spec §4.3). Step 2 approves weight
changes only. Steps 3 and 4 add the event kinds to both sets when their consumers ship, so
no approval can create an event nothing reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import ValidationError

from imageshield.intel.cells import StaleReason, published_unacknowledged, stale_reason
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.proposal_models import (
    ContextSignal,
    ProposalRecord,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.vocabulary import ScoringVocabulary

WhyNot = Literal["not_decidable", "evidence_retracted", "uncorroborated", "tags_unmapped"]

APPROVABLE_KINDS: frozenset[str] = frozenset({"weight_change"})
# A coverage_gap can only be dismissed (§4.7); a weight_suggestion is never decidable.
REJECTABLE_KINDS: frozenset[str] = frozenset({"weight_change", "coverage_gap"})
EVENT_KINDS: frozenset[str] = frozenset({"threat_event", "protection_event"})


def _target_tags(target: dict[str, Any]) -> list[str]:
    tags = target.get("tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def unmapped_tags(target: dict[str, Any], vocabulary: ScoringVocabulary | None) -> list[str]:
    mapped = vocabulary.mapped_tags if vocabulary is not None else frozenset()
    return [t for t in _target_tags(target) if t not in mapped]


def retired_tags(target: dict[str, Any], vocabulary: ScoringVocabulary | None) -> list[str]:
    retired = vocabulary.registry().retired if vocabulary is not None else frozenset()
    return [t for t in _target_tags(target) if t in retired]


def all_tags_unmapped(target: dict[str, Any], vocabulary: ScoringVocabulary | None) -> bool:
    """§4.5: a non-global event whose tags are ALL unmapped waits, unapprovable. A partly
    mapped event is approvable."""
    if target.get("is_global"):
        return False
    tags = _target_tags(target)
    return bool(tags) and len(unmapped_tags(target, vocabulary)) == len(tags)


def why_not(
    proposal: ProposalRecord,
    active_signals: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
) -> WhyNot | None:
    if proposal.kind not in APPROVABLE_KINDS:
        return "not_decidable"
    if not active_signals:
        return "evidence_retracted"
    if uncorroborated(active_signals):
        return "uncorroborated"
    if proposal.kind in EVENT_KINDS and all_tags_unmapped(proposal.target, vocabulary):
        return "tags_unmapped"
    return None


def read_flags(
    proposal: ProposalRecord,
    linked_signals: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
) -> dict[str, Any]:
    """The read-time fields of both proposal reads.

    ``approvable`` is "the decision would not 409": pending, and ``why_not`` null. A pending
    weight_change can read ``stale`` in the window between a push and its reconcile. It stays
    approvable by that rule, and the approval answers 422 values_out_of_bounds (§4.5 re-check).
    """
    active = [s for s in linked_signals if s.status == "active"]
    why = why_not(proposal, active, vocabulary)
    why_stale: StaleReason | None = None
    pending_ack = False
    if (
        proposal.kind == "weight_change"
        and proposal.status in ("pending", "approved")
        and vocabulary is not None
    ):
        target: WeightChangeTarget | None
        decided: WeightDelta | None
        try:
            target = WeightChangeTarget.model_validate(proposal.target)
            decided = (
                WeightDelta.model_validate(proposal.decided)
                if proposal.status == "approved" and proposal.decided is not None
                else None
            )
        except ValidationError:
            target, decided = None, None
        if target is not None:
            if decided is not None:
                pending_ack = published_unacknowledged(
                    vocabulary,
                    question_key=target.question_key,
                    option=target.option,
                    current=target.current,
                    delta=decided.delta,
                )
            if not pending_ack:
                why_stale = stale_reason(
                    vocabulary,
                    question_key=target.question_key,
                    option=target.option,
                    current=target.current,
                    generated_at_release=(
                        proposal.against_release_no
                        if proposal.against_release_no is not None
                        else -1
                    ),
                )
    return {
        "approvable": proposal.status == "pending" and why is None,
        "why_not": why,
        "evidence_retracted": not active,
        "stale": why_stale is not None,
        "why_stale": why_stale,
        "applied_pending_ack": pending_ack,
        "unmapped_tags": unmapped_tags(proposal.target, vocabulary),
        "retired_tags": retired_tags(proposal.target, vocabulary),
    }
