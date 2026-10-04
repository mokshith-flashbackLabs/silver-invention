"""One body of evidence, one proposal (spec 2026-10-04-intel-evidence-quality §5). Pure."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from imageshield.intel.overlap import OverlapCandidate, overlaps_of, pool_of, proposal_tags
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord
from tests.intel_fakes import scoring

NOW = datetime(2026, 10, 4, tzinfo=UTC)
V = scoring()  # platforms/Instagram is mapped to instagram; nothing else is mapped


def _signal(document: str, *excerpts: str) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="incident",
        direction="risk_up",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust="web",
        publisher_domain="news.example",
        status="active",
        created_at=NOW,
        document_key=document,
        excerpts=excerpts,
    )


def _record(kind: str, target: dict[str, Any]) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=uuid4(),
        kind=kind,
        status="pending",
        target=target,
        suggested={},
        decided=None,
        against_release_no=None,
        created_at=NOW,
    )


INSTAGRAM_CHANGE = {"question_key": "platforms", "option": "Instagram", "current": 3}


def _pool(*items: tuple[ProposalRecord, list[ContextSignal]]) -> list[OverlapCandidate]:
    return pool_of([r for r, _ in items], {r.proposal_id: s for r, s in items}, V)


def _ids(found: dict[UUID, list[dict[str, Any]]], record: ProposalRecord) -> set[UUID]:
    return {o["proposal_id"] for o in found[record.proposal_id]}


def test_a_weight_change_and_a_threat_on_the_same_reports_overlap_both_ways() -> None:
    """The dev case: +1 on Instagram and a temporary Instagram threat from the same pages."""
    shared = [_signal("page-a"), _signal("page-b")]
    change = _record("weight_change", INSTAGRAM_CHANGE)
    threat = _record("threat_event", {"tags": ["instagram"]})
    found = overlaps_of(_pool((change, shared), (threat, [*shared, _signal("page-c")])))
    assert found[change.proposal_id] == [
        {"proposal_id": threat.proposal_id, "kind": "threat_event"}
    ]
    assert found[threat.proposal_id] == [
        {"proposal_id": change.proposal_id, "kind": "weight_change"}
    ]


def test_a_protection_and_a_risk_framed_from_one_fact_overlap() -> None:
    fact = _signal("meta-labels")
    protection = _record("protection_event", {"tags": ["instagram"], "is_global": False})
    change = _record("weight_change", INSTAGRAM_CHANGE)
    found = overlaps_of(_pool((protection, [fact]), (change, [fact, _signal("other")])))
    assert _ids(found, protection) == {change.proposal_id}


def test_half_of_either_ones_evidence_is_enough_and_less_is_not() -> None:
    a_docs = [_signal(d) for d in ("1", "2", "3", "4")]
    threat = _record("threat_event", {"tags": ["instagram"]})
    change = _record("weight_change", INSTAGRAM_CHANGE)
    # one shared page of four each: 25% of either, no overlap
    found = overlaps_of(
        _pool((threat, a_docs), (change, [_signal("1"), _signal("x"), _signal("y"), _signal("z")]))
    )
    assert found[threat.proposal_id] == []
    # one shared page is half of a two-page proposal
    found = overlaps_of(_pool((threat, a_docs), (change, [_signal("1"), _signal("x")])))
    assert _ids(found, threat) == {change.proposal_id}


def test_echoing_pages_are_one_unit_of_evidence() -> None:
    sentence = (
        "Starting today the platform will let people request removal of synthetic content that"
        " simulates their face or voice."
    )
    threat = _record("threat_event", {"tags": ["instagram"]})
    change = _record("weight_change", INSTAGRAM_CHANGE)
    found = overlaps_of(
        _pool(
            (threat, [_signal("outlet-a", sentence)]),
            (change, [_signal("outlet-b", '"' + sentence.upper() + '"'), _signal("unrelated")]),
        )
    )
    assert _ids(found, threat) == {change.proposal_id}


def test_no_overlap_without_a_shared_tag_or_between_two_weight_changes() -> None:
    shared = [_signal("page")]
    linkedin = _record("threat_event", {"tags": ["linkedin"]})
    change = _record("weight_change", INSTAGRAM_CHANGE)
    other_cell = _record("weight_change", {**INSTAGRAM_CHANGE, "option": "Instagram"})
    unmapped = _record("weight_change", {**INSTAGRAM_CHANGE, "option": "LinkedIn"})
    found = overlaps_of(
        _pool((linkedin, shared), (change, shared), (other_cell, shared), (unmapped, shared))
    )
    assert all(found[r.proposal_id] == [] for r in (linkedin, change, other_cell, unmapped))
    assert proposal_tags(unmapped, V) == frozenset()


def test_two_pending_events_of_one_kind_on_one_scope_overlap_but_a_renewal_never_does() -> None:
    """A duplicate written before 2026-10-04 (the two YouTube protections on dev) is shown and
    covered like any overlap. A renewal continues an approved credit and is never covered."""
    quote = _signal("influencer-hub")
    first = _record("protection_event", {"tags": ["instagram"], "is_global": False})
    second = _record("protection_event", {"tags": ["instagram"], "is_global": False})
    renewal = _record(
        "protection_event",
        {"tags": ["instagram"], "is_global": False, "renews_event_id": str(uuid4())},
    )
    pool = _pool((first, [quote, _signal("other")]), (second, [quote]), (renewal, [quote]))
    assert len(pool) == 2  # the renewal is no candidate
    found = overlaps_of(pool)
    assert _ids(found, first) == {second.proposal_id}
    assert _ids(found, second) == {first.proposal_id}


def test_only_limits_which_proposals_are_answered() -> None:
    shared = [_signal("page")]
    threat = _record("threat_event", {"tags": ["instagram"]})
    change = _record("weight_change", INSTAGRAM_CHANGE)
    found = overlaps_of(_pool((threat, shared), (change, shared)), only=[threat.proposal_id])
    assert list(found) == [threat.proposal_id]
