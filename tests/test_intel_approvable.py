"""The approvability predicate (spec §3.6, §4.5, INVARIANTS #50). Pure: no database."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from imageshield.intel.approvable import (
    all_tags_unmapped,
    read_flags,
    retired_tags,
    unmapped_tags,
    why_not,
)
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord
from imageshield.intel.publisher import publisher_domain
from tests.intel_fakes import QUIZ_VOCABULARY, scoring

NOW = datetime.now(UTC)


def _signal(
    publisher: str = "a.example", *, trust: str = "web", status: str = "active"
) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="policy",
        direction="risk_up",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust=trust,  # type: ignore[arg-type]
        publisher_domain=publisher,
        status=status,
        created_at=NOW,
    )


def _proposal(
    kind: str = "weight_change",
    status: str = "pending",
    target: dict[str, Any] | None = None,
    decided: dict[str, Any] | None = None,
) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=uuid4(),
        kind=kind,
        status=status,
        target=target or {"question_key": "platforms", "option": "Instagram", "current": 3},
        suggested={"delta": 1},
        decided=decided,
        against_release_no=2,
        created_at=NOW,
    )


def test_one_web_publisher_is_uncorroborated() -> None:
    assert uncorroborated([_signal("news.example")])


def test_two_subdomains_of_one_publisher_are_one_publisher() -> None:
    a = publisher_domain("https://a.news.example.co.uk/x")
    b = publisher_domain("https://b.news.example.co.uk/y")
    assert a == b
    assert uncorroborated([_signal(a), _signal(b)])


def test_two_publishers_corroborate() -> None:
    assert not uncorroborated([_signal("a.example"), _signal("b.example")])


def test_a_listed_signal_corroborates_on_its_own() -> None:
    assert not uncorroborated([_signal(trust="listed")])


def test_a_retracted_signal_does_not_count() -> None:
    assert uncorroborated([_signal("a.example"), _signal("b.example", status="retracted")])


def test_why_not_answers_in_the_spec_order() -> None:
    v = scoring()
    # not_decidable first: a coverage gap is only dismissed, a suggestion never decided
    assert (
        why_not(_proposal(kind="coverage_gap", target={"subject": "Bumble"}), [], v)
        == "not_decidable"
    )
    assert (
        why_not(_proposal(kind="weight_suggestion", status="delivered"), [], v) == "not_decidable"
    )
    protection = _proposal(
        kind="protection_event", target={"tags": ["linkedin"], "is_global": False}
    )
    assert why_not(protection, [], v) == "evidence_retracted"
    assert why_not(protection, [_signal(trust="listed")], v) == "tags_unmapped"
    everyone = _proposal(kind="protection_event", target={"tags": [], "is_global": True})
    assert why_not(everyone, [_signal(trust="listed")], v) is None  # reaches everyone
    threat = _proposal(kind="threat_event", target={"tags": ["linkedin"]})
    assert why_not(threat, [], v) == "evidence_retracted"
    assert why_not(threat, [_signal(trust="listed")], v) == "tags_unmapped"
    mapped = _proposal(kind="threat_event", target={"tags": ["instagram", "linkedin"]})
    assert why_not(mapped, [_signal(trust="listed")], v) is None  # partly mapped: approvable
    assert why_not(_proposal(), [], v) == "evidence_retracted"
    assert why_not(_proposal(), [_signal("a.example")], v) == "uncorroborated"
    assert why_not(_proposal(), [_signal("a.example"), _signal("b.example")], v) is None


def test_tag_helpers_steps_3_and_4_reuse() -> None:
    v = scoring()  # instagram mapped, linkedin unmapped, myspace retired
    assert unmapped_tags({"tags": ["instagram", "linkedin"]}, v) == ["linkedin"]
    assert retired_tags({"tags": ["myspace", "instagram"]}, v) == ["myspace"]
    assert unmapped_tags({"question_key": "platforms"}, v) == []
    assert all_tags_unmapped({"tags": ["linkedin"]}, v)
    assert not all_tags_unmapped({"tags": ["instagram", "linkedin"]}, v)  # partly mapped
    assert not all_tags_unmapped({"tags": [], "is_global": True}, v)


def test_a_pending_corroborated_change_reads_approvable() -> None:
    flags = read_flags(_proposal(), [_signal("a.example"), _signal("b.example")], scoring())
    assert flags["approvable"] is True and flags["why_not"] is None
    assert flags["stale"] is False and flags["applied_pending_ack"] is False
    assert flags["evidence_retracted"] is False


def test_an_approved_change_published_but_unacknowledged_is_never_stale() -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"]["Instagram"] = 4  # 3 + 1, published
    proposal = _proposal(status="approved", decided={"delta": 1})
    flags = read_flags(proposal, [_signal(trust="listed")], scoring(doc, release_no=3))
    assert flags["applied_pending_ack"] is True and flags["stale"] is False
    assert flags["approvable"] is False  # not pending


def test_an_approved_change_whose_cell_moved_reads_stale() -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"]["Instagram"] = 5
    proposal = _proposal(status="approved", decided={"delta": 1})
    flags = read_flags(proposal, [_signal(trust="listed")], scoring(doc, release_no=3))
    assert flags["stale"] is True and flags["why_stale"] == "deduction_moved"
    assert flags["applied_pending_ack"] is False


def test_no_active_signal_reads_evidence_retracted() -> None:
    flags = read_flags(_proposal(), [_signal(status="retracted")], scoring())
    assert flags["evidence_retracted"] is True and flags["why_not"] == "evidence_retracted"
    assert flags["approvable"] is False


def test_a_renewal_whose_credit_ended_is_not_approvable_whatever_its_evidence() -> None:
    """Final review M3/M4: second in the fixed order, after not_decidable -- no evidence can
    make a renewal of a retracted or lapsed credit approvable."""
    renewal = _proposal("protection_event", target={"tags": ["instagram"], "renews_event_id": "x"})
    flags = read_flags(renewal, [], scoring(), renewed_credit_ended=True)
    assert (flags["approvable"], flags["why_not"]) == (False, "renewed_credit_ended")
    assert why_not(renewal, [], scoring(), renewed_credit_ended=False) == "evidence_retracted"
    gap = _proposal("coverage_gap", target={"subject": "Bumble"})
    assert why_not(gap, [], scoring(), renewed_credit_ended=True) == "not_decidable"
