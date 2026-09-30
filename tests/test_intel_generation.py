"""Proposal validation, in code (spec §4.3, §4.5, §6.3). Pure: no database, no model."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from imageshield.intel.generation import (
    GeneratedBatch,
    prompt_quiz,
    prompt_registry,
    prompt_signal,
    validate_proposals,
)
from imageshield.intel.prompts import proposal_request
from imageshield.intel.proposal_models import ContextSignal
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedCoverageGap,
    ProposedTag,
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
) -> tuple[GeneratedBatch, Counter[str]]:
    counts: Counter[str] = Counter()
    batch = validate_proposals(
        output,
        context={s.signal_id: s for s in context},
        gap_pool=gap_pool or [],
        vocabulary=V,
        now=NOW,
        counts=counts,
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
