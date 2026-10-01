"""Proposal validation for every generated kind -- step 2's weight changes and coverage gaps,
step 3's threat events and attachments, step 4's protection events -- (spec §4.3, §4.5, §6.3),
and the payload builders for the generation prompt.

Code, never the model, decides what is written. The model's output is read against the
vocabulary the run LOADED (never a row a push may have overwritten since) and against the
evidence the model was shown. A proposal that fails any rule is dropped whole, with its reason
counted on the run's outcome. Nothing is "fixed up": an out-of-range delta is not clamped, and
an unknown signal id is not skipped.

A coverage gap's evidence is not the model's claim either. It is recomputed here from every
active signal of the window that concerns the subject.

An event proposal of either kind that repeats a pending proposal is that proposal again,
decided here by rule, never by the model: same kind, same tag set, a shared signal document
(spec §4.3). It becomes an attachment of the run's own new evidence.

The model may not make a protection credit global: it cannot know that a protection applies
wherever a person lives (spec §4.5). Such a proposal is dropped (global_not_proposable); a global
credit exists only by an operator's edit on approval.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from imageshield.intel.bounds import (
    COVERAGE_GAP_MIN_PUBLISHERS,
    COVERAGE_GAP_MIN_SIGNALS,
    COVERAGE_GAP_WINDOW_DAYS,
    MAX_EVENT_TITLE_CHARS,
    MAX_GAP_SUBJECT_CHARS,
    MAX_PROMPT_TAGS,
    MAX_RATIONALE_CHARS,
    MAX_SUGGESTED_QUESTION_CHARS,
    PROTECTION_REVIEW_MAX_DAYS,
    PROTECTION_REVIEW_MIN_DAYS,
    PROTECTION_STRENGTH_MAX,
    PROTECTION_STRENGTH_MIN,
    THREAT_EXPIRES_MAX_DAYS,
    THREAT_EXPIRES_MIN_DAYS,
    THREAT_SEVERITY_MAX,
    THREAT_SEVERITY_MIN,
)
from imageshield.intel.cells import cell_problem
from imageshield.intel.pii import mask
from imageshield.intel.prompts import (
    PromptLiveEvent,
    PromptLiveProtection,
    PromptOption,
    PromptPendingEvent,
    PromptQuestion,
    PromptSignal,
    RegistryTag,
)
from imageshield.intel.proposal_models import (
    Attachment,
    ContextSignal,
    CoverageGapTarget,
    LiveEvent,
    LiveProtection,
    NewProposal,
    PendingEvent,
    ProtectionEventSuggested,
    ProtectionEventTarget,
    SuggestedTag,
    ThreatEventSuggested,
    ThreatEventTarget,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedAttach,
    ProposedCoverageGap,
    ProposedProtectionEvent,
    ProposedTag,
    ProposedThreatEvent,
    ProposedWeightChange,
)
from imageshield.intel.tags import is_well_formed, membership_problems
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject


@dataclass
class GeneratedBatch:
    weight_changes: list[NewProposal] = field(default_factory=list)
    coverage_gaps: list[NewProposal] = field(default_factory=list)
    threat_events: list[NewProposal] = field(default_factory=list)
    protection_events: list[NewProposal] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def proposals(self) -> list[NewProposal]:
        return [
            *self.weight_changes,
            *self.coverage_gaps,
            *self.threat_events,
            *self.protection_events,
        ]


def validate_proposals(
    output: ProposalOutput,
    *,
    context: Mapping[UUID, ContextSignal],
    gap_pool: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary,
    now: datetime,
    counts: Counter[str],
    pending_events: Mapping[UUID, PendingEvent] | None = None,
    new_signal_ids: Collection[UUID] = (),
    events_only: bool = False,
) -> GeneratedBatch:
    """``pending_events`` are the pending event proposals of both kinds the run loaded (those the
    prompt showed): attach targets and duplicate candidates. ``new_signal_ids`` are the run's own
    new evidence, the only signals an attachment may add. ``events_only`` is a gap_regenerate
    run, which writes event proposals and attachments and nothing else (spec §4.9)."""
    batch = GeneratedBatch()
    if events_only:
        dropped = len(output.weight_changes) + len(output.coverage_gaps)
        if dropped:
            counts["proposal_dropped_not_an_event"] += dropped
    else:
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
    pending = pending_events or {}
    for event in output.threat_events:
        proposal = _threat_event(event, context, vocabulary, counts)
        if proposal is not None:
            _keep_event(proposal, batch.threat_events, batch, pending, new_signal_ids, counts)
    for credit in output.protection_events:
        proposal = _protection_event(credit, context, vocabulary, counts)
        if proposal is not None:
            _keep_event(proposal, batch.protection_events, batch, pending, new_signal_ids, counts)
    for attach in output.attach:
        attachment = _attachment(attach, pending, context, new_signal_ids, counts)
        if attachment is not None:
            batch.attachments.append(attachment)
    return batch


def duplicate_of(proposal: NewProposal, pending: Iterable[PendingEvent]) -> UUID | None:
    """spec §4.3: a new event proposal whose kind and tag set equal a pending proposal's, and
    which shares any signal document with it, IS that proposal. A document is a page, compared
    by canonical URL hash, so a page a later run read again counts (spec note 2026-09-30). The
    first match in the order given (the store's newest first) wins."""
    tags = frozenset(proposal.target.get("tags", ()))
    documents = frozenset(proposal.document_keys)
    for candidate in pending:
        if (
            candidate.kind == proposal.kind
            and frozenset(candidate.tags) == tags
            and candidate.document_keys & documents
        ):
            return candidate.proposal_id
    return None


def _same_event(a: NewProposal, b: NewProposal) -> bool:
    """Two event proposals in one batch that duplicate_of would call one incident."""
    return (
        a.kind == b.kind
        and frozenset(a.target.get("tags", ())) == frozenset(b.target.get("tags", ()))
        and bool(set(a.document_keys) & set(b.document_keys))
    )


def _keep_event(
    proposal: NewProposal,
    kept: list[NewProposal],
    batch: GeneratedBatch,
    pending: Mapping[UUID, PendingEvent],
    new_signal_ids: Collection[UUID],
    counts: Counter[str],
) -> None:
    """spec §4.3's duplicate rule for one validated event proposal of either kind. A repeat of a
    pending proposal (same kind, same tag set, a shared document) becomes an attachment of the
    run's own new evidence, or is dropped when it cites none; a repeat of one kept earlier in
    this batch is dropped; anything else is kept."""
    duplicate = duplicate_of(proposal, pending.values())
    if duplicate is not None:
        fresh = tuple(i for i in proposal.signal_ids if i in new_signal_ids)
        if fresh:
            batch.attachments.append(Attachment(duplicate, fresh))
            counts["proposal_converted_to_attach"] += 1
        else:
            counts["proposal_dropped_duplicate_event"] += 1
        return
    if any(_same_event(proposal, earlier) for earlier in kept):
        counts["proposal_dropped_duplicate_event"] += 1
        return
    kept.append(proposal)


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


def _threat_event(
    item: ProposedThreatEvent,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
) -> NewProposal | None:
    """§4.5 for threat_event. Every tag must be a registered, non-retired slug, and a miss is
    dropped, never fixed up. A proposal whose tags are all UNMAPPED is kept: it is written
    pending and waits for a mapping (tags_unmapped is a read-time answer, intel/approvable.py)."""
    if not item.tags:
        counts["proposal_dropped_no_tags"] += 1
        return None
    if any(not is_well_formed(t) for t in item.tags) or len(set(item.tags)) != len(item.tags):
        counts["proposal_dropped_tag_malformed"] += 1
        return None
    unknown, retired = membership_problems(item.tags, vocabulary.registry())
    if unknown:
        counts["proposal_dropped_unknown_tag"] += 1
        return None
    if retired:
        counts["proposal_dropped_tag_retired"] += 1
        return None
    if not THREAT_SEVERITY_MIN <= item.severity <= THREAT_SEVERITY_MAX:
        counts["proposal_dropped_severity_out_of_bounds"] += 1
        return None
    if not THREAT_EXPIRES_MIN_DAYS <= item.expires_in_days <= THREAT_EXPIRES_MAX_DAYS:
        counts["proposal_dropped_expiry_out_of_bounds"] += 1
        return None
    signal_ids = _cited(item.signal_ids, context, counts)
    if signal_ids is None:
        return None
    title = _free_text(item.title, field_name="title", limit=MAX_EVENT_TITLE_CHARS, counts=counts)
    if title is None:
        return None
    rationale = _free_text(
        item.rationale, field_name="rationale", limit=MAX_RATIONALE_CHARS, counts=counts
    )
    if rationale is None:
        return None
    documents = tuple(
        dict.fromkeys(d for i in signal_ids if (d := context[i].document_key) is not None)
    )
    return NewProposal(
        kind="threat_event",
        target=ThreatEventTarget(tags=tuple(item.tags)).model_dump(mode="json"),
        suggested=ThreatEventSuggested(
            kind=item.kind,
            title=title,
            severity=item.severity,
            expires_in_days=item.expires_in_days,
        ).model_dump(mode="json"),
        rationale=rationale,
        signal_ids=signal_ids,
        document_keys=documents,
    )


def _protection_event(
    item: ProposedProtectionEvent,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
) -> NewProposal | None:
    """§4.5 for protection_event. A global proposal is dropped first (global_not_proposable).
    Then the threat rules: every tag a registered, non-retired slug, dropped never fixed up; a
    proposal whose tags are all UNMAPPED is kept, pending, until a mapping gives it reach."""
    if item.is_global:
        counts["proposal_dropped_global_not_proposable"] += 1
        return None
    if not item.tags:
        counts["proposal_dropped_no_tags"] += 1
        return None
    if any(not is_well_formed(t) for t in item.tags) or len(set(item.tags)) != len(item.tags):
        counts["proposal_dropped_tag_malformed"] += 1
        return None
    unknown, retired = membership_problems(item.tags, vocabulary.registry())
    if unknown:
        counts["proposal_dropped_unknown_tag"] += 1
        return None
    if retired:
        counts["proposal_dropped_tag_retired"] += 1
        return None
    if not PROTECTION_STRENGTH_MIN <= item.strength <= PROTECTION_STRENGTH_MAX:
        counts["proposal_dropped_strength_out_of_bounds"] += 1
        return None
    if not PROTECTION_REVIEW_MIN_DAYS <= item.review_in_days <= PROTECTION_REVIEW_MAX_DAYS:
        counts["proposal_dropped_review_out_of_bounds"] += 1
        return None
    signal_ids = _cited(item.signal_ids, context, counts)
    if signal_ids is None:
        return None
    title = _free_text(item.title, field_name="title", limit=MAX_EVENT_TITLE_CHARS, counts=counts)
    if title is None:
        return None
    rationale = _free_text(
        item.rationale, field_name="rationale", limit=MAX_RATIONALE_CHARS, counts=counts
    )
    if rationale is None:
        return None
    documents = tuple(
        dict.fromkeys(d for i in signal_ids if (d := context[i].document_key) is not None)
    )
    return NewProposal(
        kind="protection_event",
        target=ProtectionEventTarget(tags=tuple(item.tags)).model_dump(
            mode="json", exclude_none=True
        ),
        suggested=ProtectionEventSuggested(
            title=title, strength=item.strength, review_in_days=item.review_in_days
        ).model_dump(mode="json"),
        rationale=rationale,
        signal_ids=signal_ids,
        document_keys=documents,
    )


def _attachment(
    item: ProposedAttach,
    pending: Mapping[UUID, PendingEvent],
    context: Mapping[UUID, ContextSignal],
    new_signal_ids: Collection[UUID],
    counts: Counter[str],
) -> Attachment | None:
    """spec §4.3: the target must be a pending EVENT proposal the run loaded, and every signal
    the run's own new, active evidence. The write transaction re-checks that the target is
    still pending (intel/proposal_store.py)."""
    try:
        proposal_id = UUID(item.proposal_id)
    except ValueError:
        counts["attach_dropped_unknown_proposal"] += 1
        return None
    if proposal_id not in pending:
        counts["attach_dropped_unknown_proposal"] += 1
        return None
    if not item.signal_ids:
        counts["attach_dropped_no_signals"] += 1
        return None
    ids: list[UUID] = []
    for raw in item.signal_ids:
        try:
            signal_id = UUID(raw)
        except ValueError:
            counts["attach_dropped_signal_not_new"] += 1
            return None
        signal = context.get(signal_id)
        if signal_id not in new_signal_ids or signal is None or signal.status != "active":
            counts["attach_dropped_signal_not_new"] += 1
            return None
        if signal_id not in ids:
            ids.append(signal_id)
    return Attachment(proposal_id, tuple(ids))


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


def prompt_pending_event(event: PendingEvent) -> PromptPendingEvent:
    return PromptPendingEvent(
        proposal_id=str(event.proposal_id),
        kind=event.kind,
        title=event.title,
        severity=event.severity,
        tags=list(event.tags),
        signal_ids=[str(i) for i in event.signal_ids],
    )


def prompt_live_event(event: LiveEvent) -> PromptLiveEvent:
    return PromptLiveEvent(
        event_id=str(event.event_id),
        kind=event.kind,
        title=event.title,
        severity=event.severity,
        tags=list(event.tags),
        expires_at=event.expires_at.isoformat(),
        signal_ids=[str(i) for i in event.signal_ids],
    )


def prompt_live_protection(event: LiveProtection) -> PromptLiveProtection:
    return PromptLiveProtection(
        event_id=str(event.event_id),
        title=event.title,
        strength=event.strength,
        tags=list(event.tags),
        is_global=event.is_global,
        review_by=event.review_by.isoformat(),
        signal_ids=[str(i) for i in event.signal_ids],
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
