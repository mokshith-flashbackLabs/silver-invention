"""The approvability predicate (spec §3.6). ``why_not`` answers every refusal knowable at
read time, in a fixed order: not_decidable, renewed_credit_ended, evidence_retracted,
evidence_stale, uncorroborated, tags_unmapped.

``evidence_stale`` (2026-10-04, spec 2026-10-04-intel-evidence-quality §3) is a threat_event
whose active evidence is all dated and all older than ``INTEL_THREAT_RECENCY_DAYS``
(intel/recency.py): an old or ended incident is not a threat. Undated evidence never makes a
proposal stale. The decision refuses it ``409 proposal_evidence_stale``; it can still be rejected.

``renewed_credit_ended`` (final review M3/M4, 2026-10-01) is a pending renewal whose credit is
no longer live -- retracted, or past its review date. A renewal starts at that credit's
review_by, so approving one late would insert a credit that is live at once or already over;
the decision refuses it (409 proposal_not_pending) and both reads say so, rather than showing
approvable a proposal the decision then refuses for ever.

Both reads call it (through ``read_flags``) and so does the decision, inside its transaction.
So a proposal the panel shows as approvable is never one the decision refuses with a 409,
except for races the transaction itself catches (proposal_not_pending,
proposal_cell_awaiting_publish).

Which kinds are decidable depends on the build step (spec §4.3). Step 3 added threat_event
and step 4 protection_event, each with its consumer (svc.v_active_scoped_events). So no
approval can create an event nothing reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import ValidationError

from imageshield.intel.cells import StaleReason, published_unacknowledged, stale_reason
from imageshield.intel.corroboration import independent_sources, uncorroborated
from imageshield.intel.proposal_models import (
    ContextSignal,
    ProposalRecord,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.recency import Recency, evidence_dates, evidence_stale
from imageshield.intel.vocabulary import ScoringVocabulary

WhyNot = Literal[
    "not_decidable",
    "renewed_credit_ended",
    "evidence_retracted",
    "evidence_stale",
    "uncorroborated",
    "tags_unmapped",
]

APPROVABLE_KINDS: frozenset[str] = frozenset({"weight_change", "threat_event", "protection_event"})
# A coverage_gap can only be dismissed (§4.7); a weight_suggestion is never decidable.
REJECTABLE_KINDS: frozenset[str] = frozenset(
    {"weight_change", "coverage_gap", "threat_event", "protection_event"}
)
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
    *,
    recency: Recency,
    renewed_credit_ended: bool = False,
) -> WhyNot | None:
    """``renewed_credit_ended`` is the caller's read of the credit a pending renewal continues:
    the reads take it from ``proposal_store.ended_renewals`` and the decision from the credit
    it has locked. ``recency`` is ``INTEL_THREAT_RECENCY_DAYS`` at the moment of the read or the
    decision; required, so no caller can forget it and silently pass an old incident."""
    if proposal.kind not in APPROVABLE_KINDS:
        return "not_decidable"
    if renewed_credit_ended:
        return "renewed_credit_ended"
    if not active_signals:
        return "evidence_retracted"
    if proposal.kind == "threat_event" and evidence_stale(active_signals, recency):
        return "evidence_stale"
    if uncorroborated(active_signals):
        return "uncorroborated"
    if proposal.kind in EVENT_KINDS and all_tags_unmapped(proposal.target, vocabulary):
        return "tags_unmapped"
    return None


def read_flags(
    proposal: ProposalRecord,
    linked_signals: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
    *,
    recency: Recency,
    renewed_credit_ended: bool = False,
) -> dict[str, Any]:
    """The read-time fields of both proposal reads.

    *2026-10-04:* ``independent_sources`` is how many independent sources the active evidence
    comes from (intel/corroboration.py, the count #50 compares), and ``evidence_dates`` is
    ``{newest, oldest, undated}`` over its documents (intel/recency.py).

    ``approvable`` is "the decision would not 409": pending, and ``why_not`` null. A pending
    weight_change can read ``stale`` in the window between a push and its reconcile. It stays
    approvable by that rule, and the approval answers 422 values_out_of_bounds (§4.5 re-check).
    """
    active = [s for s in linked_signals if s.status == "active"]
    why = why_not(
        proposal, active, vocabulary, recency=recency, renewed_credit_ended=renewed_credit_ended
    )
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
    # A weight_suggestion may cite nothing (spec §3.6, note of 2026-09-30): having no evidence is
    # not having evidence retracted.
    cited_nothing = proposal.kind == "weight_suggestion" and not linked_signals
    return {
        "approvable": proposal.status == "pending" and why is None,
        "why_not": why,
        "evidence_retracted": not active and not cited_nothing,
        "stale": why_stale is not None,
        "why_stale": why_stale,
        "applied_pending_ack": pending_ack,
        "unmapped_tags": unmapped_tags(proposal.target, vocabulary),
        "retired_tags": retired_tags(proposal.target, vocabulary),
        "independent_sources": independent_sources(active),
        "evidence_dates": evidence_dates(active),
    }
