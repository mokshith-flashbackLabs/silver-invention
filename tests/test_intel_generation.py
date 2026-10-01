"""Proposal validation, in code (spec §4.3, §4.5, §6.3). Pure: no database, no model."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from imageshield.intel.generation import (
    GeneratedBatch,
    duplicate_of,
    prompt_live_event,
    prompt_pending_event,
    prompt_quiz,
    prompt_registry,
    prompt_signal,
    validate_proposals,
)
from imageshield.intel.prompts import (
    PROPOSE_PROMPT_VERSION,
    PromptLiveEvent,
    PromptLiveProtection,
    PromptPendingEvent,
    proposal_request,
)
from imageshield.intel.proposal_models import (
    Attachment,
    ContextSignal,
    LiveEvent,
    NewProposal,
    PendingEvent,
)
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedAttach,
    ProposedCoverageGap,
    ProposedTag,
    ProposedThreatEvent,
    ProposedWeightChange,
)
from tests.intel_fakes import scoring

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
V = scoring()


def _sig(
    *,
    tags: tuple[str, ...] = (),
    subjects: tuple[str, ...] = (),
    publisher: str = "a.example",
    trust: str = "listed",
    status: str = "active",
    days_old: int = 1,
    document: str | None = None,
) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="policy",
        direction="risk_up",
        tags=tags,
        unregistered_subjects=subjects,
        summary="s",
        trust=trust,  # type: ignore[arg-type]
        publisher_domain=publisher,
        status=status,
        created_at=NOW - timedelta(days=days_old),
        document_key=document,
    )


def _change(signal: ContextSignal | None, **kw: object) -> ProposedWeightChange:
    fields: dict[str, object] = {
        "question_key": "platforms",
        "option": "Instagram",
        "current": 3,
        "delta": 1,
        "rationale": "Photos now train AI by default.",
        "signal_ids": [str(signal.signal_id)] if signal else [],
    }
    fields.update(kw)
    return ProposedWeightChange.model_validate(fields)


def _validate(
    output: ProposalOutput,
    context: list[ContextSignal],
    gap_pool: list[ContextSignal] | None = None,
    *,
    pending: dict[UUID, PendingEvent] | None = None,
    new: set[UUID] | None = None,
    events_only: bool = False,
) -> tuple[GeneratedBatch, Counter[str]]:
    counts: Counter[str] = Counter()
    batch = validate_proposals(
        output,
        context={s.signal_id: s for s in context},
        gap_pool=gap_pool or [],
        vocabulary=V,
        now=NOW,
        counts=counts,
        pending_events=pending,
        new_signal_ids=frozenset(new or ()),
        events_only=events_only,
    )
    return batch, counts


def test_a_valid_change_is_kept_with_its_rationale_masked() -> None:
    s = _sig(tags=("instagram",))
    out = ProposalOutput(
        weight_changes=[_change(s, rationale="AI training by default; ask press@example.com")]
    )
    batch, counts = _validate(out, [s])
    (proposal,) = batch.weight_changes
    assert proposal.target == {"question_key": "platforms", "option": "Instagram", "current": 3}
    assert proposal.suggested == {"delta": 1} and proposal.signal_ids == (s.signal_id,)
    assert "press@example.com" not in proposal.rationale
    assert counts["pii_masked_rationale"] == 1


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"question_key": "age", "option": "Under 25", "current": 5}, "not_mutable"),
        ({"question_key": "nope"}, "unknown_question"),
        ({"option": "Bumble"}, "unknown_option"),
        ({"current": 2}, "current_mismatch"),
        ({"delta": 3}, "delta_out_of_bounds"),
        ({"delta": 0}, "delta_out_of_bounds"),
        ({"option": "Snapchat", "current": 7, "delta": 2}, "result_out_of_bounds"),
    ],
)
def test_an_invalid_change_is_never_written(kw: dict[str, object], reason: str) -> None:
    s = _sig()
    batch, counts = _validate(ProposalOutput(weight_changes=[_change(s, **kw)]), [s])
    assert batch.weight_changes == []
    assert counts[f"proposal_dropped_{reason}"] == 1


def test_one_bad_change_does_not_sink_the_good_one() -> None:
    """Review Focus 1."""
    s = _sig()
    out = ProposalOutput(
        weight_changes=[
            _change(s, delta=7),
            _change(s, option="LinkedIn", current=2, delta=-1),
        ]
    )
    batch, counts = _validate(out, [s])
    assert [p.target["option"] for p in batch.weight_changes] == ["LinkedIn"]
    assert counts["proposal_dropped_delta_out_of_bounds"] == 1


def test_a_change_citing_unknown_retracted_or_no_evidence_is_dropped() -> None:
    s, gone = _sig(), _sig(status="retracted")
    out = ProposalOutput(
        weight_changes=[
            _change(None, signal_ids=[str(uuid4())]),
            _change(None, signal_ids=[str(gone.signal_id)], option="LinkedIn", current=2),
            _change(None, signal_ids=["not-a-uuid"], option="Threads", current=1),
            _change(None, signal_ids=[], option="X (Twitter)", current=4),
        ]
    )
    batch, counts = _validate(out, [s, gone])
    assert batch.weight_changes == []
    assert counts["proposal_dropped_unknown_signal"] == 3
    assert counts["proposal_dropped_no_signals"] == 1


def test_two_changes_for_one_cell_in_one_run_keep_the_first() -> None:
    s = _sig()
    out = ProposalOutput(weight_changes=[_change(s, delta=1), _change(s, delta=2)])
    batch, counts = _validate(out, [s])
    assert [p.suggested for p in batch.weight_changes] == [{"delta": 1}]
    assert counts["proposal_dropped_duplicate_cell"] == 1


def _gap(signal: ContextSignal, subject: str = "Bumble", **kw: object) -> ProposedCoverageGap:
    fields: dict[str, object] = {
        "subject": subject,
        "rationale": "Three reports concern Bumble.",
        "signal_ids": [str(signal.signal_id)],
    }
    fields.update(kw)
    return ProposedCoverageGap.model_validate(fields)


def _bumble_pool(*publishers: str, days_old: int = 1) -> list[ContextSignal]:
    return [_sig(subjects=("Bumble",), publisher=p, days_old=days_old) for p in publishers]


def test_a_gap_needs_three_signals_from_two_publishers_in_ninety_days() -> None:
    pool = _bumble_pool("a.example", "a.example", "b.example")
    batch, _ = _validate(ProposalOutput(coverage_gaps=[_gap(pool[0])]), pool, pool)
    (gap,) = batch.coverage_gaps
    assert gap.target == {"subject": "Bumble"} and gap.suggested == {}
    assert set(gap.signal_ids) == {s.signal_id for s in pool}


@pytest.mark.parametrize(
    "pool",
    [
        _bumble_pool("a.example", "b.example"),  # two signals
        _bumble_pool("a.example", "a.example", "a.example"),  # one publisher
        [*_bumble_pool("a.example", "b.example"), *_bumble_pool("c.example", days_old=91)],
        [
            *_bumble_pool("a.example", "b.example"),
            _sig(subjects=("Bumble",), publisher="c.example", status="retracted"),
        ],
    ],
)
def test_a_gap_below_the_threshold_is_dropped(pool: list[ContextSignal]) -> None:
    batch, counts = _validate(ProposalOutput(coverage_gaps=[_gap(pool[0])]), pool, pool)
    assert batch.coverage_gaps == []
    assert counts["proposal_dropped_gap_below_threshold"] == 1


def test_a_gap_about_a_registered_but_unmapped_tag_counts_its_tagged_signals() -> None:
    pool = [_sig(tags=("linkedin",), publisher=p) for p in ("a.example", "b.example", "c.example")]
    tag = ProposedTag(slug="linkedin", label="LinkedIn", kind="platform")
    out = ProposalOutput(coverage_gaps=[_gap(pool[0], subject="LinkedIn", suggested_tag=tag)])
    batch, _ = _validate(out, pool, pool)
    (gap,) = batch.coverage_gaps
    assert gap.target["suggested_tag"] == {
        "slug": "linkedin",
        "label": "LinkedIn",
        "kind": "platform",
    }


def test_a_gap_about_a_mapped_subject_is_dropped() -> None:
    pool = [_sig(tags=("instagram",), publisher=p) for p in ("a.example", "b.example", "c.x")]
    batch, counts = _validate(
        ProposalOutput(coverage_gaps=[_gap(pool[0], subject="Instagram")]), pool, pool
    )
    assert batch.coverage_gaps == []
    assert counts["proposal_dropped_subject_already_mapped"] == 1


@pytest.mark.parametrize("slug", ["myspace", "instagram", "Bad Slug"])
def test_a_gap_suggesting_a_retired_mapped_or_malformed_tag_is_dropped(slug: str) -> None:
    pool = _bumble_pool("a.example", "b.example", "c.example")
    tag = ProposedTag(slug=slug, label="Something", kind="platform")
    batch, counts = _validate(
        ProposalOutput(coverage_gaps=[_gap(pool[0], suggested_tag=tag)]), pool, pool
    )
    assert batch.coverage_gaps == []
    assert counts["proposal_dropped_suggested_tag_invalid"] == 1


def test_gap_free_text_is_masked_and_the_gap_survives() -> None:
    pool = _bumble_pool("a.example", "b.example", "c.example")
    out = ProposalOutput(
        coverage_gaps=[_gap(pool[0], suggested_question="Do you use Bumble? Call +44 20 7946 0958")]
    )
    batch, counts = _validate(out, pool, pool)
    (gap,) = batch.coverage_gaps
    assert "7946" not in gap.target["suggested_question"]
    assert counts["pii_masked_suggested_question"] == 1


def test_two_gaps_for_one_subject_in_one_run_keep_the_first() -> None:
    pool = _bumble_pool("a.example", "b.example", "c.example")
    out = ProposalOutput(coverage_gaps=[_gap(pool[0]), _gap(pool[0], subject="bumble!")])
    batch, counts = _validate(out, pool, pool)
    assert len(batch.coverage_gaps) == 1
    assert counts["proposal_dropped_duplicate_subject"] == 1


def _threat(*signals: ContextSignal, **kw: object) -> ProposedThreatEvent:
    fields: dict[str, object] = {
        "kind": "leak",
        "title": "Instagram breach exposes private photos",
        "severity": 3,
        "expires_in_days": 30,
        "tags": ["instagram"],
        "rationale": "Several outlets report the same breach.",
        "signal_ids": [str(s.signal_id) for s in signals],
    }
    fields.update(kw)
    return ProposedThreatEvent.model_validate(fields)


def _pending(
    proposal_id: UUID,
    *,
    tags: tuple[str, ...] = ("instagram",),
    documents: frozenset[str] = frozenset(),
) -> PendingEvent:
    return PendingEvent(proposal_id, "threat_event", tags, "Breach", 3, (), documents)


def test_a_valid_threat_event_is_kept_with_its_title_masked() -> None:
    s = _sig(tags=("instagram",))
    out = ProposalOutput(threat_events=[_threat(s, title="Breach; call +44 20 7946 0958")])
    batch, counts = _validate(out, [s])
    (proposal,) = batch.threat_events
    assert proposal.kind == "threat_event" and proposal.target == {"tags": ["instagram"]}
    assert proposal.suggested["kind"] == "leak" and proposal.suggested["severity"] == 3
    assert proposal.suggested["expires_in_days"] == 30
    assert "7946" not in proposal.suggested["title"] and counts["pii_masked_title"] == 1
    assert proposal.signal_ids == (s.signal_id,) and batch.proposals == [proposal]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"tags": []}, "no_tags"),
        ({"tags": ["Instagram"]}, "tag_malformed"),
        ({"tags": ["instagram", "instagram"]}, "tag_malformed"),
        ({"tags": ["tiktok"]}, "unknown_tag"),
        ({"tags": ["instagram", "myspace"]}, "tag_retired"),
        ({"severity": 0}, "severity_out_of_bounds"),
        ({"severity": 6}, "severity_out_of_bounds"),
        ({"expires_in_days": 0}, "expiry_out_of_bounds"),
        ({"expires_in_days": 91}, "expiry_out_of_bounds"),
        ({"title": "   "}, "empty_title"),
        ({"title": "x" * 201}, "title_too_long"),
        ({"signal_ids": []}, "no_signals"),
    ],
)
def test_an_invalid_threat_event_is_never_written(kw: dict[str, object], reason: str) -> None:
    s = _sig(tags=("instagram",))
    batch, counts = _validate(ProposalOutput(threat_events=[_threat(s, **kw)]), [s])
    assert batch.threat_events == []
    assert counts[f"proposal_dropped_{reason}"] == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"severity": 1},
        {"severity": 5},
        {"expires_in_days": 1},
        {"expires_in_days": 90},
        {"title": "x" * 200},
    ],
)
def test_a_threat_event_on_a_bound_is_kept(kw: dict[str, object]) -> None:
    """The refusals above pin only the outside neighbour of each bound (0, 6, 0, 91, 201), so a
    ``<`` written where ``<=`` belongs would pass them all and silently drop every severity-1
    or one-day incident."""
    s = _sig(tags=("instagram",))
    batch, counts = _validate(ProposalOutput(threat_events=[_threat(s, **kw)]), [s])
    assert len(batch.threat_events) == 1 and not counts


def test_a_threat_on_registered_but_unmapped_tags_is_kept_to_wait() -> None:
    """spec §4.5: written pending, never dropped. tags_unmapped is a read-time answer."""
    s = _sig(tags=("linkedin",))
    batch, _ = _validate(ProposalOutput(threat_events=[_threat(s, tags=["linkedin"])]), [s])
    assert [p.target for p in batch.threat_events] == [{"tags": ["linkedin"]}]


def test_a_duplicate_of_a_pending_proposal_attaches_only_the_new_evidence() -> None:
    """Review Focus 4: same kind, same tag set, a shared document (one page) -- the pending one
    again."""
    doc = "hash-of-the-page"
    old, new = _sig(tags=("instagram",), document=doc), _sig(tags=("instagram",), document=doc)
    pid = uuid4()
    batch, counts = _validate(
        ProposalOutput(threat_events=[_threat(old, new)]),
        [old, new],
        pending={pid: _pending(pid, documents=frozenset({doc}))},
        new={new.signal_id},
    )
    assert batch.threat_events == []
    assert batch.attachments == [Attachment(pid, (new.signal_id,))]
    assert counts["proposal_converted_to_attach"] == 1


def test_a_duplicate_citing_no_new_evidence_is_dropped() -> None:
    doc = "hash-of-the-page"
    old = _sig(tags=("instagram",), document=doc)
    pid = uuid4()
    batch, counts = _validate(
        ProposalOutput(threat_events=[_threat(old)]),
        [old],
        pending={pid: _pending(pid, documents=frozenset({doc}))},
    )
    assert batch.threat_events == [] and batch.attachments == []
    assert counts["proposal_dropped_duplicate_event"] == 1


def test_another_tag_set_or_no_shared_document_is_a_new_proposal() -> None:
    doc = "hash-of-the-page"
    pid = uuid4()
    pending = {pid: _pending(pid, documents=frozenset({doc}))}  # tags == ("instagram",)
    wider = _sig(tags=("instagram", "linkedin"), document=doc)
    batch, _ = _validate(
        ProposalOutput(threat_events=[_threat(wider, tags=["instagram", "linkedin"])]),
        [wider],
        pending=pending,
        new={wider.signal_id},
    )
    assert len(batch.threat_events) == 1
    elsewhere = _sig(tags=("instagram",), document="hash-of-another-page")
    batch, _ = _validate(
        ProposalOutput(threat_events=[_threat(elsewhere)]),
        [elsewhere],
        pending=pending,
        new={elsewhere.signal_id},
    )
    assert len(batch.threat_events) == 1


def test_two_copies_of_one_incident_in_one_batch_keep_the_first() -> None:
    s = _sig(tags=("instagram",), document="hash-of-the-page")
    out = ProposalOutput(threat_events=[_threat(s), _threat(s, title="The same breach again")])
    batch, counts = _validate(out, [s])
    assert len(batch.threat_events) == 1
    assert batch.threat_events[0].suggested["title"] == "Instagram breach exposes private photos"
    assert counts["proposal_dropped_duplicate_event"] == 1


def test_incidents_that_differ_in_tags_or_in_page_are_all_kept_in_one_batch() -> None:
    """One page can report two incidents, and two pages can report two on the same platform:
    the first-wins rule needs BOTH the tag set and a page to match."""
    both = _sig(tags=("instagram", "linkedin"), document="hash-of-the-page")
    elsewhere = _sig(tags=("instagram",), document="hash-of-another-page")
    unhashed = _sig(tags=("instagram",))  # no known page, so it shares none
    out = ProposalOutput(
        threat_events=[
            _threat(both, tags=["instagram"]),
            _threat(both, tags=["linkedin"], title="The same page, another platform"),
            _threat(elsewhere, title="Another page, the same platform"),
            _threat(unhashed, title="A page nobody could identify"),
        ]
    )
    batch, counts = _validate(out, [both, elsewhere, unhashed])
    assert len(batch.threat_events) == 4 and not counts


def test_duplicate_of_needs_kind_tag_set_and_a_page_and_the_first_match_wins() -> None:
    page = "hash-of-the-page"

    def new_event(*tags: str, pages: tuple[str, ...] = (page,)) -> NewProposal:
        return NewProposal("threat_event", {"tags": list(tags)}, {}, "why", (uuid4(),), pages)

    def pending_event(*tags: str, pages: frozenset[str] = frozenset({page})) -> PendingEvent:
        return _pending(uuid4(), tags=tags, documents=pages)

    newest, older = pending_event("instagram"), pending_event("instagram")
    assert duplicate_of(new_event("instagram"), [newest, older]) == newest.proposal_id
    assert duplicate_of(new_event("instagram"), [older, newest]) == older.proposal_id
    # the tags are a set: their order is nothing, and neither a wider nor a narrower one matches
    both = pending_event("linkedin", "instagram")
    assert duplicate_of(new_event("instagram", "linkedin"), [both]) == both.proposal_id
    assert duplicate_of(new_event("instagram"), [both]) is None
    assert duplicate_of(new_event("instagram", "linkedin"), [newest]) is None
    # a page must be shared, and a proposal citing no known page shares none
    elsewhere = pending_event("instagram", pages=frozenset({"hash-of-another-page"}))
    assert duplicate_of(new_event("instagram"), [elsewhere]) is None
    assert duplicate_of(new_event("instagram", pages=()), [newest]) is None
    # and the kind must be the same
    assert duplicate_of(new_event("instagram"), [replace(newest, kind="protection_event")]) is None


def test_attach_is_validated_in_code() -> None:
    new, old = _sig(tags=("instagram",)), _sig(tags=("instagram",))
    pid = uuid4()
    out = ProposalOutput(
        attach=[
            ProposedAttach(proposal_id=str(pid), signal_ids=[str(new.signal_id)]),
            ProposedAttach(proposal_id=str(uuid4()), signal_ids=[str(new.signal_id)]),
            ProposedAttach(proposal_id="not-an-id", signal_ids=[str(new.signal_id)]),
            ProposedAttach(proposal_id=str(pid), signal_ids=[str(old.signal_id)]),
            ProposedAttach(proposal_id=str(pid), signal_ids=[]),
        ]
    )
    batch, counts = _validate(out, [new, old], pending={pid: _pending(pid)}, new={new.signal_id})
    assert batch.attachments == [Attachment(pid, (new.signal_id,))]
    assert counts["attach_dropped_unknown_proposal"] == 2
    assert counts["attach_dropped_signal_not_new"] == 1
    assert counts["attach_dropped_no_signals"] == 1


def test_an_attach_is_dropped_whole_for_a_retracted_absent_or_malformed_signal() -> None:
    """The signals must be the run's own new, ACTIVE evidence. One bad signal drops the whole
    attach, nothing is trimmed to the good ones, and a repeated id is collapsed."""
    fresh, gone = _sig(tags=("instagram",)), _sig(tags=("instagram",), status="retracted")
    ghost = uuid4()  # named as new evidence, but not in the context the model was shown
    pid = uuid4()
    out = ProposalOutput(
        attach=[
            ProposedAttach(proposal_id=str(pid), signal_ids=[str(gone.signal_id)]),
            ProposedAttach(proposal_id=str(pid), signal_ids=[str(ghost)]),
            ProposedAttach(proposal_id=str(pid), signal_ids=["not-an-id"]),
            ProposedAttach(
                proposal_id=str(pid), signal_ids=[str(fresh.signal_id), str(gone.signal_id)]
            ),
            ProposedAttach(proposal_id=str(pid), signal_ids=[str(fresh.signal_id)] * 2),
        ]
    )
    batch, counts = _validate(
        out,
        [fresh, gone],
        pending={pid: _pending(pid)},
        new={fresh.signal_id, gone.signal_id, ghost},
    )
    assert batch.attachments == [Attachment(pid, (fresh.signal_id,))]
    assert counts["attach_dropped_signal_not_new"] == 4


def test_a_regeneration_writes_events_and_nothing_else() -> None:
    s = _sig(tags=("instagram",))
    pool = _bumble_pool("a.example", "b.example", "c.example")
    out = ProposalOutput(
        weight_changes=[_change(s)], coverage_gaps=[_gap(pool[0])], threat_events=[_threat(s)]
    )
    batch, counts = _validate(out, [s, *pool], pool, events_only=True)
    assert batch.weight_changes == [] and batch.coverage_gaps == []
    assert len(batch.threat_events) == 1
    assert counts["proposal_dropped_not_an_event"] == 2


def test_the_prompt_carries_the_quiz_with_mutability_and_no_person_data() -> None:
    s = _sig(tags=("instagram",))
    system, user = proposal_request(
        [prompt_signal(s)],
        [],
        quiz=prompt_quiz(V),
        registry_tags=prompt_registry(V, {"instagram"}),
        mapped_tags=sorted(V.mapped_tags),
    )
    payload = json.loads(user)
    platforms = payload["quiz"][0]
    assert platforms["key"] == "platforms" and platforms["mutable"] is True
    assert platforms["options"][0] == {"option": "Instagram", "deduction": 3, "tags": ["instagram"]}
    assert payload["quiz"][2]["mutable"] is False  # escrowed age
    assert UUID(payload["new_evidence"][0]["signal_id"]) == s.signal_id
    assert payload["mapped_tags"] == ["instagram"]
    blob = (system + user).lower()
    assert "user_ref" not in blob and "phone" not in blob
    assert "myspace" not in {t["slug"] for t in payload["tag_registry"]}  # retired omitted


def test_the_prompt_carries_pending_and_live_events_and_the_events_only_rule() -> None:
    pending = PromptPendingEvent(
        proposal_id=str(uuid4()),
        kind="threat_event",
        title="Breach",
        severity=3,
        tags=["instagram"],
        signal_ids=[],
    )
    live = PromptLiveEvent(
        event_id=str(uuid4()),
        kind="leak",
        title="Leak",
        severity=4,
        tags=["instagram"],
        expires_at="2026-10-30T00:00:00+00:00",
        signal_ids=[],
    )

    def build(events_only: bool) -> tuple[str, str]:
        return proposal_request(
            [],
            [],
            quiz=prompt_quiz(V),
            registry_tags=prompt_registry(V, set()),
            mapped_tags=sorted(V.mapped_tags),
            pending_events=[pending],
            live_events=[live],
            events_only=events_only,
        )

    system, user = build(False)
    payload = json.loads(user)
    assert payload["pending_events"] == [pending] and payload["live_events"] == [live]
    assert "threat_events" in system and "attach" in system
    assert "Propose only threat_events, protection_events and attach" not in system
    regenerate, _ = build(True)
    assert regenerate.startswith(system)
    assert "Propose only threat_events, protection_events and attach" in regenerate
    assert PROPOSE_PROMPT_VERSION == "propose-v3"


def test_the_event_prompt_items_carry_ids_as_strings() -> None:
    pid, sid, eid = uuid4(), uuid4(), uuid4()
    pending = PendingEvent(pid, "threat_event", ("instagram",), "Breach", 3, (sid,), frozenset())
    assert prompt_pending_event(pending) == {
        "proposal_id": str(pid),
        "kind": "threat_event",
        "title": "Breach",
        "severity": 3,
        "tags": ["instagram"],
        "signal_ids": [str(sid)],
    }
    ends = NOW + timedelta(days=7)
    live = LiveEvent(eid, "leak", "Leak", 4, ("instagram",), False, NOW, ends, None, (sid,))
    item = prompt_live_event(live)
    assert item["event_id"] == str(eid) and item["expires_at"] == ends.isoformat()
    assert item["signal_ids"] == [str(sid)] and item["severity"] == 4


def test_the_prompt_carries_live_protections_and_asks_for_protection_events() -> None:
    live = PromptLiveProtection(
        event_id=str(uuid4()),
        title="Instagram opt-out from AI training",
        strength=2,
        tags=["instagram"],
        is_global=False,
        review_by="2027-03-30T00:00:00+00:00",
        signal_ids=[],
    )
    system, user = proposal_request(
        [],
        [],
        quiz=prompt_quiz(V),
        registry_tags=prompt_registry(V, set()),
        mapped_tags=sorted(V.mapped_tags),
        live_protections=[live],
    )
    payload = json.loads(user)
    assert payload["live_protections"] == [live] and payload["live_events"] == []
    assert "protection_events" in system and "is_global: always false" in system
    assert "only in some countries" in system and "live_protections" in system
    assert PROPOSE_PROMPT_VERSION == "propose-v3"
