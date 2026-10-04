"""One body of evidence, one proposal (spec 2026-10-04-intel-evidence-quality §5).

The same evidence used to produce a lasting ``weight_change`` AND a temporary ``threat_event`` on
the same platform, or one fact framed both as a ``protection_event`` and as a reason to raise a
weight. Each such proposal is still written, but every pending proposal names the pending
proposals it OVERLAPS (``overlaps`` on both reads), and approving one supersedes every proposal
it overlaps, in the decision's transaction, with ``supersede_reason = 'covered_by_decision'``.
Rejecting one changes nothing else.

Two pending proposals overlap when:

- **they could be the same fact**: different kinds among weight_change, threat_event and
  protection_event, or the same EVENT kind (a duplicate generation could not catch, such as one
  written before 2026-10-04). Two weight changes never overlap: each is its own cell, and the
  newer-proposal rule already keeps one per cell. A renewal never overlaps: it continues a credit
  an operator already approved;
- **they concern an overlapping tag**: an event's ``target.tags``, a weight change's option's
  mapped tags in the live vocabulary (an option with none overlaps nothing);
- **they rest on the same evidence**: the evidence UNITS they share are at least
  ``OVERLAP_MIN_SHARE`` of either one's units. A unit is a document (by canonical URL hash) with
  every document whose excerpts echo it (intel/corroboration.py's echo rule): "half of either's
  documents by canonical URL, or the echo groups". A company's other domains are NOT collapsed
  here: two different announcements by one company are two pieces of evidence, even though they
  are one voice for corroboration.

Pure: the reads and the decision load the pending pool and call ``overlaps_of``.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from imageshield.intel.bounds import OVERLAP_MIN_SHARE
from imageshield.intel.corroboration import UnionFind, excerpt_text, unite_echoes
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord
from imageshield.intel.vocabulary import ScoringVocabulary

OVERLAP_KINDS: frozenset[str] = frozenset({"weight_change", "threat_event", "protection_event"})


@dataclass(frozen=True)
class OverlapCandidate:
    """A pending proposal as the overlap check sees it: its tags and its ACTIVE evidence."""

    proposal_id: UUID
    kind: str
    tags: frozenset[str]
    signals: tuple[ContextSignal, ...]


def proposal_tags(record: ProposalRecord, vocabulary: ScoringVocabulary | None) -> frozenset[str]:
    """The tags a proposal concerns: an event's own, or a weight change's option's mapped tags."""
    target: dict[str, Any] = record.target or {}
    if record.kind == "weight_change":
        if vocabulary is None:
            return frozenset()
        key = (str(target.get("question_key", "")), str(target.get("option", "")))
        return frozenset(vocabulary.option_tags.get(key, ()))
    tags = target.get("tags")
    if not isinstance(tags, list):
        return frozenset()
    return frozenset(t for t in tags if isinstance(t, str))


def candidate(
    record: ProposalRecord,
    linked: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
) -> OverlapCandidate | None:
    """``record`` as an overlap candidate, or None for a kind that never overlaps or a renewal.
    The caller decides which statuses it passes (pending ones, plus the proposal being decided)."""
    if record.kind not in OVERLAP_KINDS:
        return None
    if record.kind == "protection_event" and (record.target or {}).get("renews_event_id"):
        return None
    return OverlapCandidate(
        proposal_id=record.proposal_id,
        kind=record.kind,
        tags=proposal_tags(record, vocabulary),
        signals=tuple(s for s in linked if s.status == "active"),
    )


def _could_be_one_fact(a: OverlapCandidate, b: OverlapCandidate) -> bool:
    if a.kind == b.kind:
        return a.kind != "weight_change"
    return True


def _units(candidates: Iterable[OverlapCandidate]) -> dict[UUID, Hashable]:
    """Every signal's evidence unit across the whole pool: one per document, joined by echoes."""
    groups = UnionFind()
    seen: dict[UUID, ContextSignal] = {}
    for proposal in candidates:
        for signal in proposal.signals:
            seen.setdefault(signal.signal_id, signal)
    for signal in seen.values():
        document = signal.document_key or str(signal.signal_id)
        groups.union(("signal", signal.signal_id), ("document", document))
    unite_echoes(
        groups,
        (
            (("signal", signal.signal_id), excerpt_text(quote))
            for signal in seen.values()
            for quote in signal.excerpts
        ),
    )
    return {sid: groups.find(("signal", sid)) for sid in seen}


def _rests_on_the_same_evidence(a: frozenset[Hashable], b: frozenset[Hashable]) -> bool:
    shared = len(a & b)
    if not shared:
        return False
    return shared >= OVERLAP_MIN_SHARE * len(a) or shared >= OVERLAP_MIN_SHARE * len(b)


def overlaps_of(
    pool: Sequence[OverlapCandidate], *, only: Iterable[UUID] | None = None
) -> dict[UUID, list[dict[str, Any]]]:
    """For each proposal in ``only`` (every one in ``pool`` when None), the proposals of ``pool``
    it overlaps, newest first as ``pool`` is ordered: ``[{proposal_id, kind}]``."""
    units = _units(pool)
    unit_sets = {p.proposal_id: frozenset(units[s.signal_id] for s in p.signals) for p in pool}
    wanted = set(only) if only is not None else {p.proposal_id for p in pool}
    found: dict[UUID, list[dict[str, Any]]] = {pid: [] for pid in wanted}
    for a in pool:
        if a.proposal_id not in wanted:
            continue
        for b in pool:
            if (
                b.proposal_id != a.proposal_id
                and _could_be_one_fact(a, b)
                and a.tags & b.tags
                and _rests_on_the_same_evidence(unit_sets[a.proposal_id], unit_sets[b.proposal_id])
            ):
                found[a.proposal_id].append({"proposal_id": b.proposal_id, "kind": b.kind})
    return found


def pool_of(
    records: Sequence[ProposalRecord],
    linked: Mapping[UUID, Sequence[ContextSignal]],
    vocabulary: ScoringVocabulary | None,
) -> list[OverlapCandidate]:
    """The overlap candidates among ``records``, in their order."""
    pool: list[OverlapCandidate] = []
    for record in records:
        made = candidate(record, linked.get(record.proposal_id, ()), vocabulary)
        if made is not None:
            pool.append(made)
    return pool
