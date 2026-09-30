"""Proposal validation for the step-2 kinds (spec §4.3, §4.5, §6.3), and the payload builders
for the generation prompt.

Code, never the model, decides what is written. The model's output is read against the
vocabulary the run LOADED (never a row a push may have overwritten since) and against the
evidence the model was shown. A proposal that fails any rule is dropped whole, with its reason
counted on the run's outcome. Nothing is "fixed up": an out-of-range delta is not clamped, and
an unknown signal id is not skipped.

A coverage gap's evidence is not the model's claim either. It is recomputed here from every
active signal of the window that concerns the subject.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from imageshield.intel.bounds import (
    COVERAGE_GAP_MIN_PUBLISHERS,
    COVERAGE_GAP_MIN_SIGNALS,
    COVERAGE_GAP_WINDOW_DAYS,
    MAX_GAP_SUBJECT_CHARS,
    MAX_PROMPT_TAGS,
    MAX_RATIONALE_CHARS,
    MAX_SUGGESTED_QUESTION_CHARS,
)
from imageshield.intel.cells import cell_problem
from imageshield.intel.pii import mask
from imageshield.intel.prompts import PromptOption, PromptQuestion, PromptSignal, RegistryTag
from imageshield.intel.proposal_models import (
    ContextSignal,
    CoverageGapTarget,
    NewProposal,
    SuggestedTag,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedCoverageGap,
    ProposedTag,
    ProposedWeightChange,
)
from imageshield.intel.tags import is_well_formed
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject


@dataclass
class GeneratedBatch:
    weight_changes: list[NewProposal] = field(default_factory=list)
    coverage_gaps: list[NewProposal] = field(default_factory=list)

    @property
    def proposals(self) -> list[NewProposal]:
        return [*self.weight_changes, *self.coverage_gaps]


def validate_proposals(
    output: ProposalOutput,
    *,
    context: Mapping[UUID, ContextSignal],
    gap_pool: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary,
    now: datetime,
    counts: Counter[str],
) -> GeneratedBatch:
    batch = GeneratedBatch()
    cells: set[tuple[str, str]] = set()
    for item in output.weight_changes:
        proposal = _weight_change(item, context, vocabulary, counts)
        if proposal is None:
            continue
        cell = (proposal.target["question_key"], proposal.target["option"])
        if cell in cells:
            counts["proposal_dropped_duplicate_cell"] += 1
            continue
        cells.add(cell)
        batch.weight_changes.append(proposal)
    subjects: set[str] = set()
    for gap in output.coverage_gaps:
        proposal = _coverage_gap(gap, context, gap_pool, vocabulary, now, counts)
        if proposal is None:
            continue
        key = normalise_subject(proposal.target["subject"])
        if key in subjects:
            counts["proposal_dropped_duplicate_subject"] += 1
            continue
        subjects.add(key)
        batch.coverage_gaps.append(proposal)
    return batch


def concerns(
    signal: ContextSignal,
    subject_key: str,
    suggested_slug: str | None,
    vocabulary: ScoringVocabulary,
) -> bool:
    """spec §4.5: a signal concerns a gap's subject when one of its unregistered subjects
    normalises to it, or one of its UNMAPPED tags is the suggested tag or normalises to the
    subject by slug or label. Exact, never fuzzy (§4.9's comparison)."""
    if any(normalise_subject(s) == subject_key for s in signal.unregistered_subjects):
        return True
    for tag in signal.tags:
        if tag in vocabulary.mapped_tags:
            continue
        entry = vocabulary.tags.get(tag)
        label = entry.label if entry is not None else tag
        if tag == suggested_slug or subject_key in (
            normalise_subject(tag),
            normalise_subject(label),
        ):
            return True
    return False


def _free_text(raw: str, *, field_name: str, limit: int, counts: Counter[str]) -> str | None:
    """Masked (spec §6.3), whitespace-normalised, and dropped -- never truncated -- past
    ``limit``."""
    masked, masks = mask(raw)
    if masks:
        counts[f"pii_masked_{field_name}"] += 1
    text = normalise(masked)
    if not text:
        counts[f"proposal_dropped_empty_{field_name}"] += 1
        return None
    if len(text) > limit:
        counts[f"proposal_dropped_{field_name}_too_long"] += 1
        return None
    return text


def _cited(
    raw_ids: Sequence[str], context: Mapping[UUID, ContextSignal], counts: Counter[str]
) -> tuple[UUID, ...] | None:
    if not raw_ids:
        counts["proposal_dropped_no_signals"] += 1
        return None
    cited: list[UUID] = []
    for raw in raw_ids:
        try:
            signal_id = UUID(raw)
        except ValueError:
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        signal = context.get(signal_id)
        if signal is None or signal.status != "active":
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        if signal_id not in cited:
            cited.append(signal_id)
    return tuple(cited)


def _weight_change(
    item: ProposedWeightChange,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
) -> NewProposal | None:
    problem = cell_problem(
        vocabulary,
        question_key=item.question_key,
        option=item.option,
        current=item.current,
        delta=item.delta,
    )
    if problem is not None:
        counts[f"proposal_dropped_{problem}"] += 1
        return None
    signal_ids = _cited(item.signal_ids, context, counts)
    if signal_ids is None:
        return None
    rationale = _free_text(
        item.rationale, field_name="rationale", limit=MAX_RATIONALE_CHARS, counts=counts
    )
    if rationale is None:
        return None
    return NewProposal(
        kind="weight_change",
        target=WeightChangeTarget(
            question_key=item.question_key, option=item.option, current=item.current
        ).model_dump(),
        suggested=WeightDelta(delta=item.delta).model_dump(),
        rationale=rationale,
        signal_ids=signal_ids,
    )


def _suggested_tag(
    item: ProposedTag, vocabulary: ScoringVocabulary, counts: Counter[str]
) -> SuggestedTag | None:
    """Well-formed and unregistered, or registered but unmapped (§4.5). A retired tag is
    never added to a new proposal (§3.1)."""
    masked, masks = mask(item.label)
    if masks:
        counts["pii_masked_suggested_tag"] += 1
    label = normalise(masked)
    if (
        not is_well_formed(item.slug)
        or item.slug in vocabulary.registry().retired
        or item.slug in vocabulary.mapped_tags
        or not label
    ):
        counts["proposal_dropped_suggested_tag_invalid"] += 1
        return None
    return SuggestedTag(slug=item.slug, label=label, kind=item.kind)


def _coverage_gap(
    item: ProposedCoverageGap,
    context: Mapping[UUID, ContextSignal],
    gap_pool: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary,
    now: datetime,
    counts: Counter[str],
) -> NewProposal | None:
    subject = _free_text(
        item.subject, field_name="subject", limit=MAX_GAP_SUBJECT_CHARS, counts=counts
    )
    if subject is None:
        return None
    subject_key = normalise_subject(subject)
    if not subject_key:
        counts["proposal_dropped_empty_subject"] += 1
        return None
    if vocabulary.subject_is_mapped(subject_key):
        counts["proposal_dropped_subject_already_mapped"] += 1
        return None
    suggested_tag: SuggestedTag | None = None
    if item.suggested_tag is not None:
        suggested_tag = _suggested_tag(item.suggested_tag, vocabulary, counts)
        if suggested_tag is None:
            return None
    question: str | None = None
    if item.suggested_question is not None and item.suggested_question.strip():
        question = _free_text(
            item.suggested_question,
            field_name="suggested_question",
            limit=MAX_SUGGESTED_QUESTION_CHARS,
            counts=counts,
        )
        if question is None:
            return None
    if _cited(item.signal_ids, context, counts) is None:
        return None
    rationale = _free_text(
        item.rationale, field_name="rationale", limit=MAX_RATIONALE_CHARS, counts=counts
    )
    if rationale is None:
        return None
    since = now - timedelta(days=COVERAGE_GAP_WINDOW_DAYS)
    slug = suggested_tag.slug if suggested_tag is not None else None
    qualifying = [
        s
        for s in gap_pool
        if s.status == "active"
        and s.created_at >= since
        and concerns(s, subject_key, slug, vocabulary)
    ]
    if (
        len(qualifying) < COVERAGE_GAP_MIN_SIGNALS
        or len({s.publisher_domain for s in qualifying}) < COVERAGE_GAP_MIN_PUBLISHERS
    ):
        counts["proposal_dropped_gap_below_threshold"] += 1
        return None
    target = CoverageGapTarget(
        subject=subject, suggested_tag=suggested_tag, suggested_question=question
    )
    return NewProposal(
        kind="coverage_gap",
        target=target.model_dump(mode="json", exclude_none=True),
        suggested={},
        rationale=rationale,
        signal_ids=tuple(s.signal_id for s in qualifying),
    )


def prompt_signal(signal: ContextSignal) -> PromptSignal:
    return PromptSignal(
        signal_id=str(signal.signal_id),
        category=signal.category,
        direction=signal.direction,
        tags=list(signal.tags),
        unregistered_subjects=list(signal.unregistered_subjects),
        summary=signal.summary,
        publisher=signal.publisher_domain,
        trust=signal.trust,
    )


def prompt_quiz(vocabulary: ScoringVocabulary) -> list[PromptQuestion]:
    return [
        PromptQuestion(
            key=q.key,
            prompt=q.prompt,
            mutable=q.mutable,
            cap=q.cap,
            options=[
                PromptOption(
                    option=o,
                    deduction=q.deductions.get(o),
                    tags=list(vocabulary.option_tags.get((q.key, o), ())),
                )
                for o in q.options
            ],
        )
        for q in vocabulary.questions.values()
    ]


def prompt_registry(vocabulary: ScoringVocabulary, relevant: set[str]) -> list[RegistryTag]:
    """Retired tags omitted. Past ``MAX_PROMPT_TAGS`` it narrows to ``relevant`` (the evidence's
    tags plus the mapped ones), the rule extraction uses (spec §4.3)."""
    entries = [e for e in vocabulary.tags.values() if not e.retired]
    if len(entries) > MAX_PROMPT_TAGS:
        entries = [e for e in entries if e.slug in relevant][:MAX_PROMPT_TAGS]
    return [RegistryTag(slug=e.slug, label=e.label, description=e.description) for e in entries]
