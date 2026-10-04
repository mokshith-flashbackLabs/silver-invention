"""The approvability predicate (spec §3.6, §4.5, INVARIANTS #50). Pure: no database."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import uuid4

from imageshield.intel import approvable
from imageshield.intel.approvable import (
    all_tags_unmapped,
    retired_tags,
    unmapped_tags,
)
from imageshield.intel.corroboration import independent_sources, uncorroborated
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord
from imageshield.intel.publisher import publisher_domain
from imageshield.intel.recency import Recency
from tests.intel_fakes import QUIZ_VOCABULARY, scoring

NOW = datetime.now(UTC)
RECENCY = Recency(NOW, 90)
# The predicate and the read flags at NOW with a 90-day window.
why_not = partial(approvable.why_not, recency=RECENCY)
read_flags = partial(approvable.read_flags, recency=RECENCY)


def _signal(
    publisher: str = "a.example",
    *,
    trust: str = "web",
    status: str = "active",
    excerpts: tuple[str, ...] = (),
    published: datetime | None = None,
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
        document_key=uuid4().hex,
        published_at=published,
        excerpts=excerpts,
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


# ── corroboration that ignores copies (INVARIANTS #50 amended 2026-10-04) ─────────────────────

# YouTube's own announcement, as four outlets quoted it on dev (2026-10-03): one source.
ANNOUNCEMENT = (
    "Starting today, YouTube will let people request the removal of AI-generated or other"
    " synthetic or altered content that simulates an identifiable individual, including their"
    " face or voice."
)


def test_one_sentence_quoted_by_four_outlets_is_one_source() -> None:
    echoes = [
        _signal("techradar.com", excerpts=(ANNOUNCEMENT,)),
        _signal("petapixel.com", excerpts=(f'"{ANNOUNCEMENT.upper()}"',)),
        _signal("musicbusinessworldwide.com", excerpts=("YouTube said: " + ANNOUNCEMENT,)),
        _signal("completemusicupdate.com", excerpts=(ANNOUNCEMENT.replace(",", " --"),)),
    ]
    assert independent_sources(echoes) == 1
    assert uncorroborated(echoes)


def test_echoes_chain_and_a_shorter_echo_counts_by_word_overlap() -> None:
    short = "YouTube will let people request removal of AI-generated content simulating faces"
    reworded = "People may now request removal of AI-generated content simulating faces on YouTube"
    signals = [_signal("a.example", excerpts=(short,)), _signal("b.example", excerpts=(reworded,))]
    assert independent_sources(signals) == 1  # most of the shorter excerpt's words, reordered


def test_a_companys_own_domains_are_one_publisher() -> None:
    google = [
        _signal("google.com", excerpts=("Support page text about privacy complaints.",)),
        _signal("blog.youtube", excerpts=("Our blog announces a separate disclosure tool.",)),
    ]
    assert independent_sources(google) == 1 and uncorroborated(google)
    # As stored: the registrable domain of the fetcher's final URL (intel/publisher.py).
    meta = [
        _signal(publisher_domain(url))
        for url in (
            "https://about.fb.com/news",
            "https://transparency.fb.com/x",
            "https://www.instagram.com/",
        )
    ]
    assert independent_sources(meta) == 1


def test_two_genuinely_different_publishers_still_corroborate() -> None:
    signals = [
        _signal("techradar.com", excerpts=("TechRadar tested the removal form and it took days.",)),
        _signal("theverge.com", excerpts=("A spokesperson confirmed the policy covers voices.",)),
    ]
    assert independent_sources(signals) == 2 and not uncorroborated(signals)
    assert not uncorroborated([_signal("techradar.com"), _signal("google.com")])


def test_a_listed_signal_still_corroborates_alone_however_it_echoes() -> None:
    signals = [
        _signal("pimeyes.com", trust="listed", excerpts=(ANNOUNCEMENT,)),
        _signal("techradar.com", excerpts=(ANNOUNCEMENT,)),
    ]
    assert independent_sources(signals) == 1 and not uncorroborated(signals)


def test_short_excerpts_sharing_common_words_are_not_echoes() -> None:
    signals = [
        _signal("a.example", excerpts=("The new tool is available now.",)),
        _signal("b.example", excerpts=("The tool is now available.",)),
    ]
    assert independent_sources(signals) == 2


def test_the_reads_expose_independent_sources_and_evidence_dates() -> None:
    sept, octo = datetime(2026, 9, 12, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    signals = [
        _signal("techradar.com", excerpts=(ANNOUNCEMENT,), published=sept),
        _signal("petapixel.com", excerpts=(ANNOUNCEMENT,), published=octo),
        _signal("theverge.com", excerpts=("Different words entirely, reported independently.",)),
    ]
    flags = read_flags(_proposal(), signals, scoring())
    assert flags["independent_sources"] == 2 and flags["why_not"] is None
    assert flags["evidence_dates"] == {"newest": "2026-10-02", "oldest": "2026-09-12", "undated": 1}


# ── recency (spec 2026-10-04-intel-evidence-quality §3) ────────────────────────────────────────

OLD = NOW - timedelta(days=200)


def test_a_threat_whose_evidence_is_all_dated_and_old_reads_evidence_stale() -> None:
    threat = _proposal(kind="threat_event", target={"tags": ["instagram"]})
    old = [_signal("a.example", published=OLD), _signal("b.example", published=OLD)]
    assert why_not(threat, old, scoring()) == "evidence_stale"
    flags = read_flags(threat, old, scoring())
    assert (flags["approvable"], flags["why_not"]) == (False, "evidence_stale")
    # Before uncorroborated in the fixed order: one old web publisher is stale, not uncorroborated.
    assert why_not(threat, old[:1], scoring()) == "evidence_stale"


def test_undated_or_recent_evidence_keeps_a_threat_approvable() -> None:
    threat = _proposal(kind="threat_event", target={"tags": ["instagram"]})
    undated = [_signal("a.example", published=OLD), _signal("b.example")]
    assert why_not(threat, undated, scoring()) is None
    recent = [_signal("a.example", published=OLD), _signal("b.example", published=NOW)]
    assert why_not(threat, recent, scoring()) is None


def test_only_a_threat_can_be_stale() -> None:
    old = [_signal("a.example", published=OLD), _signal("b.example", published=OLD)]
    assert why_not(_proposal(), old, scoring()) is None  # a lasting weight change
    protection = _proposal("protection_event", target={"tags": ["instagram"], "is_global": False})
    assert why_not(protection, old, scoring()) is None
