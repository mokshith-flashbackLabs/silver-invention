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
*Amended 2026-10-04 (spec 2026-10-04-intel-evidence-quality §5):* a tag set inside the other's
(one a subset of the other) counts as the same, and the write re-checks under its lock against
every pending proposal of the kind on the tags, not only those the run loaded
(intel/proposal_store.py). A threat_event whose cited evidence is all dated and older than
``INTEL_THREAT_RECENCY_DAYS`` is dropped (``evidence_stale``): an old or ended incident is not a
threat, and the decision would refuse it.

The model may not make a protection credit global: it cannot know that a protection applies
wherever a person lives (spec §4.5). Such a proposal is dropped (global_not_proposable); a global
credit exists only by an operator's edit on approval.

*2026-10-13 (spec 2026-10-13-intel-quiz-questions §3.3):* a quiz_question is written only by a pass
that was given a QuestionContext (``questions``); any other pass drops what the model returns
(``questions_closed``). Its evidence bar is recounted here over the signals it cites, and it never
repeats a live key, nor the key or subject of an open or recently dismissed question.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from imageshield.intel.bounds import (
    COVERAGE_GAP_MIN_PUBLISHERS,
    COVERAGE_GAP_MIN_SIGNALS,
    COVERAGE_GAP_WINDOW_DAYS,
    MAX_ACTION_LINK_CHARS,
    MAX_ACTION_LINK_LABEL_CHARS,
    MAX_ACTION_STEP_CHARS,
    MAX_ACTION_STEPS,
    MAX_ACTION_TITLE_CHARS,
    MAX_ACTION_WHY_CHARS,
    MAX_CARD_LINE_CHARS,
    MAX_EVENT_BODY_CHARS,
    MAX_EVENT_TITLE_CHARS,
    MAX_GAP_SUBJECT_CHARS,
    MAX_PROMPT_TAGS,
    MAX_QUESTION_OPTION_CHARS,
    MAX_QUESTION_PROMPT_CHARS,
    MAX_QUESTION_SUBJECT_CHARS,
    MAX_RATIONALE_CHARS,
    MAX_SUGGESTED_QUESTION_CHARS,
    MAX_WEIGHT_REASON_CHARS,
    PROTECTION_REVIEW_MAX_DAYS,
    PROTECTION_REVIEW_MIN_DAYS,
    PROTECTION_STRENGTH_MAX,
    PROTECTION_STRENGTH_MIN,
    QUESTION_KEY_PATTERN,
    THREAT_EXPIRES_MAX_DAYS,
    THREAT_EXPIRES_MIN_DAYS,
    THREAT_SEVERITY_MAX,
    THREAT_SEVERITY_MIN,
)
from imageshield.intel.cells import cell_problem
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.lessons import general_lesson, lesson_from
from imageshield.intel.limits import QuestionLimits
from imageshield.intel.pii import contains_pii, mask
from imageshield.intel.prompts import (
    PromptLiveEvent,
    PromptLiveProtection,
    PromptOpenQuestion,
    PromptOption,
    PromptPendingEvent,
    PromptPendingWeightChange,
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
    OpenQuestion,
    PendingEvent,
    PendingWeightChange,
    ProtectionEventSuggested,
    ProtectionEventTarget,
    QuestionContext,
    QuestionOptionSuggested,
    QuizQuestionSuggested,
    QuizQuestionTarget,
    SuggestedTag,
    ThreatActionSuggested,
    ThreatEventSuggested,
    ThreatEventTarget,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.recency import Recency, evidence_stale
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedAttach,
    ProposedCoverageGap,
    ProposedLesson,
    ProposedProtectionEvent,
    ProposedQuizQuestion,
    ProposedTag,
    ProposedThreatAction,
    ProposedThreatEvent,
    ProposedWeightChange,
)
from imageshield.intel.tags import is_well_formed, membership_problems
from imageshield.intel.text import normalise
from imageshield.intel.user_copy import (
    copy_problems,
    question_copy_problems,
    unquoted_figures,
)
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject
from imageshield.search.urlhash import url_hash


@dataclass
class GeneratedBatch:
    weight_changes: list[NewProposal] = field(default_factory=list)
    coverage_gaps: list[NewProposal] = field(default_factory=list)
    threat_events: list[NewProposal] = field(default_factory=list)
    protection_events: list[NewProposal] = field(default_factory=list)
    quiz_questions: list[NewProposal] = field(default_factory=list)
    # General lessons (spec 2026-10-15-intel-lessons-and-evidence §5); an event's own lesson rides
    # on its proposal's suggested.lesson.
    lessons: list[NewProposal] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def proposals(self) -> list[NewProposal]:
        return [
            *self.weight_changes,
            *self.coverage_gaps,
            *self.threat_events,
            *self.protection_events,
            *self.quiz_questions,
            *self.lessons,
        ]


def validate_proposals(
    output: ProposalOutput,
    *,
    context: Mapping[UUID, ContextSignal],
    gap_pool: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary,
    now: datetime,
    counts: Counter[str],
    recency: Recency,
    pending_events: Mapping[UUID, PendingEvent] | None = None,
    new_signal_ids: Collection[UUID] = (),
    events_only: bool = False,
    questions: QuestionContext | None = None,
    question_limits: QuestionLimits | None = None,
    open_lesson_topics: Collection[str] = (),
) -> GeneratedBatch:
    """``pending_events`` are the pending event proposals of both kinds the run loaded (those the
    prompt showed): attach targets and duplicate candidates. ``new_signal_ids`` are the run's own
    new evidence, the only signals an attachment may add. ``events_only`` is a gap_regenerate
    run, which writes event proposals and attachments and nothing else (spec §4.9). ``recency``
    is ``INTEL_THREAT_RECENCY_DAYS`` now: a threat whose evidence is all older is dropped.
    ``questions`` (2026-10-13) is set only for a pass that may propose quiz questions, with
    ``question_limits``; without it every proposed question is dropped (``questions_closed``)."""
    batch = GeneratedBatch()
    if questions is None or question_limits is None or events_only:
        if output.quiz_questions:
            counts["proposal_dropped_questions_closed"] += len(output.quiz_questions)
    else:
        seen = (*questions.open, *questions.dismissed)
        taken_keys = {q.question_key for q in seen} | set(vocabulary.questions)
        taken_subjects = {normalise_subject(q.subject) for q in seen}
        room = max(0, question_limits.max_pending - len(questions.open))
        for proposed in output.quiz_questions:
            question = _quiz_question(
                proposed,
                context,
                vocabulary,
                question_limits,
                now,
                counts,
                taken_keys,
                taken_subjects,
            )
            if question is None:
                continue
            if len(batch.quiz_questions) >= room:
                counts["proposal_dropped_question_cap"] += 1
                continue
            taken_keys.add(question.target["question_key"])
            taken_subjects.add(normalise_subject(question.target["subject"]))
            batch.quiz_questions.append(question)
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
    # propose-v14 (spec 2026-10-15-intel-lessons-and-evidence §4): an event's lesson is keyed by its
    # position in the events call's answer, T1... for threats and P1... for protections.
    event_lessons = {item.event: item.lesson for item in output.event_lessons}
    keys = {f"T{i}" for i in range(1, len(output.threat_events) + 1)} | {
        f"P{i}" for i in range(1, len(output.protection_events) + 1)
    }
    unknown_keys = [key for key in event_lessons if key not in keys]
    if unknown_keys:
        counts["lesson_event_unknown"] += len(unknown_keys)
    for index, event in enumerate(output.threat_events, start=1):
        proposal = _threat_event(
            event, context, vocabulary, counts, recency, lesson=event_lessons.get(f"T{index}")
        )
        if proposal is not None:
            _keep_event(proposal, batch.threat_events, batch, pending, new_signal_ids, counts)
    for index, credit in enumerate(output.protection_events, start=1):
        proposal = _protection_event(
            credit, context, vocabulary, counts, lesson=event_lessons.get(f"P{index}")
        )
        if proposal is not None:
            _keep_event(proposal, batch.protection_events, batch, pending, new_signal_ids, counts)
    if events_only:
        if output.lessons:
            counts["proposal_dropped_not_an_event"] += len(output.lessons)
    else:
        topics = set(open_lesson_topics)
        for general in output.lessons:
            lesson = general_lesson(
                general, context, vocabulary, counts, taken_topics=topics, recency=recency
            )
            if lesson is not None:
                topics.add(lesson.target["topic"])
                batch.lessons.append(lesson)
    for attach in output.attach:
        attachment = _attachment(attach, pending, context, new_signal_ids, counts)
        if attachment is not None:
            batch.attachments.append(attachment)
    return batch


def same_scope(a: Iterable[str], b: Iterable[str]) -> bool:
    """Two tag sets that describe one incident or protection: equal, or one inside the other
    (2026-10-04: {youtube} and {youtube, google} are the same YouTube feature). Two empty sets are
    never the same scope: a proposal with no tags is about nothing to compare."""
    left, right = frozenset(a), frozenset(b)
    return bool(left and right) and (left <= right or right <= left)


def duplicate_of(proposal: NewProposal, pending: Iterable[PendingEvent]) -> UUID | None:
    """spec §4.3: a new event proposal whose kind matches a pending proposal's, whose tag set is
    the same scope (``same_scope``), and which shares any signal document with it, IS that
    proposal. A document is a page, compared by canonical URL hash (``intel_documents.url_hash``),
    never by document row: a page a later run read again is a new row with the same hash (spec
    note 2026-09-30). The first match in the order given (the store's newest first) wins."""
    tags = proposal.target.get("tags", ())
    documents = frozenset(proposal.document_keys)
    for candidate in pending:
        if (
            candidate.kind == proposal.kind
            and same_scope(candidate.tags, tags)
            and candidate.document_keys & documents
        ):
            return candidate.proposal_id
    return None


def _same_event(a: NewProposal, b: NewProposal) -> bool:
    """Two event proposals in one batch that duplicate_of would call one incident."""
    return (
        a.kind == b.kind
        and same_scope(a.target.get("tags", ()), b.target.get("tags", ()))
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
    # The run's own new evidence among its citations: what the write attaches if, under its lock,
    # it finds a pending duplicate the run never loaded (intel/proposal_store.py).
    kept.append(
        replace(
            proposal,
            fresh_signal_ids=tuple(i for i in proposal.signal_ids if i in new_signal_ids),
        )
    )


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


def _event_body(raw: str, counts: Counter[str], *, limit: int = MAX_EVENT_BODY_CHARS) -> str:
    """An event's "what it means for you" (spec 2026-10-06-intel-event-body), or a points
    change's reason (``limit`` MAX_WEIGHT_REASON_CHARS, spec 2026-10-06-intel-weight-reason):
    masked and whitespace-normalised like every model-written field. Unlike the title it is never
    a reason to drop the proposal: empty or over-long, it is LEFT OUT (``""``, counted), never
    truncated, and the operator can write one before approving."""
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_body"] += 1
    text = normalise(masked)
    if not text:
        counts["event_body_missing"] += 1
        return ""
    if len(text) > limit:
        counts["event_body_too_long"] += 1
        return ""
    return text


def _card_line(raw: str, cited: Sequence[ContextSignal], counts: Counter[str]) -> str:
    """A threat's card line (spec 2026-10-11-intel-score-insights §2.2, §4): masked and normalised,
    and left out ("", counted), never truncated. A missing line keeps the threat off the score
    page's card. It never drops the threat."""
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_card_line"] += 1
    text = normalise(masked)
    if not text:
        counts["card_line_missing"] += 1
        return ""
    if len(text) > MAX_CARD_LINE_CHARS:
        counts["card_line_too_long"] += 1
        return ""
    if copy_problems(text):
        counts["card_line_forbidden_words"] += 1
        return ""
    if unquoted_figures(text, [quote for signal in cited for quote in signal.excerpts]):
        counts["card_line_figure_not_quoted"] += 1
        return ""
    return text


def _action_text(raw: str, limit: int, counts: Counter[str]) -> str | None:
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_action"] += 1
    text = normalise(masked)
    return text if text and len(text) <= limit else None


def _threat_action(
    item: ProposedThreatAction | None,
    cited: Sequence[ContextSignal],
    counts: Counter[str],
) -> dict[str, object] | None:
    """A threat's drafted recommended action (spec 2026-10-06-intel-threat-action), or None.

    Every text is masked and normalised like any model text. Anything out of bounds drops the
    ACTION, never the threat: the operator can still write one, and a threat with no action is the
    state every threat had before. The link is the one part with its own rule: it is kept only when
    it is https and is one of THIS threat's cited evidence pages (compared by canonical URL hash),
    because a model can invent a plausible URL and this one reaches a victim. Any other link is
    dropped with its label, and the action kept."""
    if item is None:
        return None
    title = _action_text(item.title, MAX_ACTION_TITLE_CHARS, counts)
    why = _action_text(item.why, MAX_ACTION_WHY_CHARS, counts)
    steps = [_action_text(step, MAX_ACTION_STEP_CHARS, counts) for step in item.steps]
    if (
        title is None
        or why is None
        or not steps
        or len(steps) > MAX_ACTION_STEPS
        or any(step is None for step in steps)
    ):
        counts["action_dropped_out_of_bounds"] += 1
        return None
    action: dict[str, object] = {"title": title, "why": why, "steps": steps}
    link = (item.link_url or "").strip()
    if link:
        pages = {s.document_key for s in cited if s.document_key is not None}
        if (
            link.startswith("https://")
            and len(link) <= MAX_ACTION_LINK_CHARS
            and url_hash(link) in pages
        ):
            action["link_url"] = link
            label = _action_text(item.link_label or "", MAX_ACTION_LINK_LABEL_CHARS, counts)
            if label is not None:
                action["link_label"] = label
        else:
            counts["action_link_not_cited"] += 1
    # exclude_none: an absent link is ABSENT, never null, so the backend's action schema (where
    # the link is optional, not nullable) reads it as it reads a hand-written one.
    return ThreatActionSuggested.model_validate(action).model_dump(mode="json", exclude_none=True)


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
        suggested=WeightDelta(
            delta=item.delta, body=_event_body(item.body, counts, limit=MAX_WEIGHT_REASON_CHARS)
        ).model_dump(),
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


def _question_text(
    raw: str, *, field_name: str, limit: int, counts: Counter[str]
) -> str | None:
    """A question's or an answer's text: ``_free_text``, and the question word rules (no figure
    check: a question quotes nothing, and "18-24" is a fine answer)."""
    text = _free_text(raw, field_name=field_name, limit=limit, counts=counts)
    if text is None:
        return None
    if question_copy_problems(text):
        counts[f"proposal_dropped_{field_name}_forbidden_words"] += 1
        return None
    return text


def _quiz_question(
    item: ProposedQuizQuestion,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    limits: QuestionLimits,
    now: datetime,
    counts: Counter[str],
    taken_keys: Collection[str],
    taken_subjects: Collection[str],
) -> NewProposal | None:
    """spec 2026-10-13-intel-quiz-questions §3.3. ``taken_keys`` are the live quiz's keys and those
    of every open, recently dismissed or earlier-in-batch question; ``taken_subjects`` their
    normalised subjects. Any failure drops the whole question, counted, never fixed up."""
    if not re.fullmatch(QUESTION_KEY_PATTERN, item.question_key):
        counts["proposal_dropped_question_key_malformed"] += 1
        return None
    if item.question_key in taken_keys:
        counts["proposal_dropped_question_key_taken"] += 1
        return None
    if contains_pii(item.question_key):
        # A key is stored and published as the quiz's own; it is never masked, so it is refused.
        counts["proposal_dropped_question_key_pii"] += 1
        return None
    subject = _free_text(
        item.subject, field_name="subject", limit=MAX_QUESTION_SUBJECT_CHARS, counts=counts
    )
    if subject is None:
        return None
    subject_key = normalise_subject(subject)
    if not subject_key:
        counts["proposal_dropped_empty_subject"] += 1
        return None
    if subject_key in taken_subjects:
        counts["proposal_dropped_question_subject_taken"] += 1
        return None
    prompt = _question_text(
        item.prompt, field_name="question", limit=MAX_QUESTION_PROMPT_CHARS, counts=counts
    )
    if prompt is None:
        return None
    if not 2 <= len(item.options) <= limits.max_options:
        counts["proposal_dropped_question_option_count"] += 1
        return None
    options: list[QuestionOptionSuggested] = []
    registry = vocabulary.registry()
    for raw in item.options:
        text = _question_text(
            raw.option, field_name="option", limit=MAX_QUESTION_OPTION_CHARS, counts=counts
        )
        if text is None:
            return None
        if any(not is_well_formed(t) for t in raw.tags) or len(set(raw.tags)) != len(raw.tags):
            counts["proposal_dropped_tag_malformed"] += 1
            return None
        unknown, retired = membership_problems(raw.tags, registry)
        if unknown:
            counts["proposal_dropped_unknown_tag"] += 1
            return None
        if retired:
            counts["proposal_dropped_tag_retired"] += 1
            return None
        new_tag: SuggestedTag | None = None
        if raw.new_tag is not None:
            new_tag = _new_tag(raw.new_tag, vocabulary, counts)
            if new_tag is None:
                return None
        options.append(QuestionOptionSuggested(option=text, tags=tuple(raw.tags), new_tag=new_tag))
    if len({o.option.casefold() for o in options}) != len(options):
        counts["proposal_dropped_question_options_repeat"] += 1
        return None
    signal_ids = _cited(item.signal_ids, context, counts)
    if signal_ids is None:
        return None
    since = now - timedelta(days=limits.window_days)
    recent = [context[i] for i in signal_ids if context[i].created_at >= since]
    if (
        len(recent) < limits.min_signals
        or len({s.publisher_domain for s in recent}) < limits.min_publishers
    ):
        counts["proposal_dropped_question_below_threshold"] += 1
        return None
    if uncorroborated([context[i] for i in signal_ids]):
        # The decision's own predicate (echoes and a company's own domains collapse): a question
        # it would refuse is never written, so no research run is spent on it (final review).
        counts["proposal_dropped_question_uncorroborated"] += 1
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
        kind="quiz_question",
        target=QuizQuestionTarget(question_key=item.question_key, subject=subject).model_dump(
            mode="json", exclude_none=True
        ),
        suggested=QuizQuestionSuggested(
            prompt=prompt, multi=item.multi, options=tuple(options)
        ).model_dump(mode="json"),
        rationale=rationale,
        signal_ids=signal_ids,
        document_keys=documents,
    )


def _new_tag(
    item: ProposedTag, vocabulary: ScoringVocabulary, counts: Counter[str]
) -> SuggestedTag | None:
    """An answer's proposed new tag: well-formed, NOT registered (a registered one belongs in
    ``tags``), with a label. Registering it stays an operator's action in the backend."""
    masked, masks = mask(item.label)
    if masks:
        counts["pii_masked_suggested_tag"] += 1
    label = normalise(masked)
    if not is_well_formed(item.slug) or item.slug in vocabulary.tags or not label:
        counts["proposal_dropped_new_tag_invalid"] += 1
        return None
    return SuggestedTag(slug=item.slug, label=label, kind=item.kind)


def _threat_event(
    item: ProposedThreatEvent,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
    recency: Recency,
    *,
    lesson: ProposedLesson | None = None,
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
    if evidence_stale([context[i] for i in signal_ids], recency):
        counts["proposal_dropped_evidence_stale"] += 1
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
        suggested={
            **ThreatEventSuggested(
                kind=item.kind,
                title=title,
                body=_event_body(item.body, counts),
                card_line=_card_line(item.card_line, [context[i] for i in signal_ids], counts),
                severity=item.severity,
                expires_in_days=item.expires_in_days,
            ).model_dump(mode="json", exclude={"action", "lesson"}),
            "action": _threat_action(item.action, [context[i] for i in signal_ids], counts),
            "lesson": lesson_from(lesson, [context[i] for i in signal_ids], counts),
        },
        rationale=rationale,
        signal_ids=signal_ids,
        document_keys=documents,
    )


def _protection_event(
    item: ProposedProtectionEvent,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
    *,
    lesson: ProposedLesson | None = None,
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
        suggested={
            **ProtectionEventSuggested(
                title=title,
                body=_event_body(item.body, counts),
                strength=item.strength,
                review_in_days=item.review_in_days,
            ).model_dump(mode="json", exclude={"lesson"}),
            "lesson": lesson_from(lesson, [context[i] for i in signal_ids], counts),
        },
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
    the run's own new, active evidence that concerns the target (``_concerns_target``). The
    write transaction re-checks that the target is still pending (intel/proposal_store.py)."""
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
        if not _concerns_target(signal, pending[proposal_id]):
            counts["attach_dropped_unrelated"] += 1
            return None
        if signal_id not in ids:
            ids.append(signal_id)
    return Attachment(proposal_id, tuple(ids))


def _concerns_target(signal: ContextSignal, target: PendingEvent) -> bool:
    """The code floor under an attach (final review M2, 2026-10-01). Corroboration counts every
    linked signal's publisher, so the model's word that a second domain "backs" a proposal must
    not be enough on its own: a TAGGED signal shares a tag with the target, and an untagged one,
    which has no tag to compare, shares a category with the target's own active evidence."""
    if signal.tags:
        return bool(set(signal.tags) & set(target.tags))
    return signal.category in target.categories


def prompt_signal(signal: ContextSignal) -> PromptSignal:
    """``published`` (propose-v4) is the document's publication date, ``YYYY-MM-DD``, or
    ``"undated"``: the model needs it to tell a current incident from an old one."""
    published = (
        signal.published_at.astimezone(UTC).date().isoformat()
        if signal.published_at is not None
        else "undated"
    )
    return PromptSignal(
        signal_id=str(signal.signal_id),
        category=signal.category,
        direction=signal.direction,
        tags=list(signal.tags),
        unregistered_subjects=list(signal.unregistered_subjects),
        summary=signal.summary,
        publisher=signal.publisher_domain,
        url=signal.document_url or "",
        trust=signal.trust,
        published=published,
        quotes=list(signal.excerpts),
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


def prompt_pending_weight_change(change: PendingWeightChange) -> PromptPendingWeightChange:
    return PromptPendingWeightChange(
        proposal_id=str(change.proposal_id),
        question_key=change.question_key,
        option=change.option,
        delta=change.delta,
        signal_ids=[str(i) for i in change.signal_ids],
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


def prompt_open_question(question: OpenQuestion) -> PromptOpenQuestion:
    return PromptOpenQuestion(
        proposal_id=str(question.proposal_id),
        status=question.status,
        question_key=question.question_key,
        subject=question.subject,
        prompt=question.prompt,
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
