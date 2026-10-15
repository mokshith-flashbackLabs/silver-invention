"""Decisions on intel proposals (spec §4.7). This is the ONE module that can move a proposal
to 'approved' (INVARIANTS #48; tests/test_boundaries.py holds it to that).

A decision is one transaction:
- the proposal row is taken FOR UPDATE;
- the vocabulary and the linked signals are read;
- the same predicate the reads use (intel/approvable.py) is re-checked, then §4.5 against the
  CURRENT vocabulary on the operator's final values;
- the transition is written guarded by its from-status;
- the audit row goes in the same transaction.

Two reviewers therefore produce one outcome and one clean 409, and two approvals of
different proposals for one cell meet the partial unique index
(intel_proposals_one_approved_per_cell): one success and one clean
proposal_cell_awaiting_publish.

Approving a threat_event (step 3) goes straight to 'applied': the same transaction inserts the
threat_events row from ``decided`` alone, with intel's own SQL (the threat store is not
importable from intel/), and records the event id as ``applied_ref``. The event is on
svc.v_active_scoped_events the moment this commits, which is what the backend reads next.

Approving a protection_event (step 4) also goes straight to 'applied', inserting the credit from
``decided`` alone. It requires the operator's ``applies_regardless_of_location: true``: a
protection limited to some places is rejected, never approved (spec §3.7). A renewal starts at
the old credit's review_by, and the old credit is locked AFTER the proposal -- the order the
retraction takes too (intel/protection_store.py) -- so a racing retraction is seen, never
deadlocked on. It is approvable only while that credit is still LIVE, active and before its
review date: so the new credit always starts in the future and approving it moves nobody, and
a late or orphaned renewal is refused (409 proposal_not_pending) exactly as both reads report
it (``why_not = renewed_credit_ended``; final review M3/M4, 2026-10-01).

*2026-10-04 (spec 2026-10-04-intel-evidence-quality §5).* Approving a proposal supersedes, in the
same transaction, every pending proposal it OVERLAPS -- the same body of evidence on an
overlapping tag (intel/overlap.py), exactly the ``overlaps`` both reads show -- with
``supersede_reason = 'covered_by_decision'``, and names them on ``Decided.superseded`` and the
audit row. Rejecting supersedes nothing. Every decision takes ``PROPOSAL_WRITE_LOCK`` first, as
the generation write and the reconcile do, so two approvals that overlap each other serialise
(the second answers 409 proposal_not_pending) instead of deadlocking.

The acknowledgement (``mark_applied``) is the second system write (spec §4.7). It moves only
approved weight changes, keeps the approval's decided_by, decided_at and decision_reason, and
is audited only for rows that actually move.

*2026-10-13 (spec 2026-10-13-intel-quiz-questions §5).* A quiz_question is approved like a weight
change -- to 'approved', waiting for the backend to publish it -- once its research run has ended.
Each answer's points default to the newest delivered suggestion for its key; an answer with no
number from either is ``question_incomplete``. The acknowledgement moves approved questions too,
and an approved question the live quiz already shows is never withdrawn.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from imageshield.intel.approvable import (
    APPROVABLE_KINDS,
    REJECTABLE_KINDS,
    all_tags_unmapped,
    why_not,
)
from imageshield.intel.cells import cell_problem, published_unacknowledged
from imageshield.intel.lesson_store import (
    carry_lesson,
    event_kind,
    insert_lesson,
    snapshot_evidence,
)
from imageshield.intel.lessons import lesson_problems
from imageshield.intel.overlap import overlaps_of
from imageshield.intel.proposal_models import (
    AppliedResult,
    ContextSignal,
    Decided,
    DecisionRefusal,
    DecisionRefused,
    LessonDecided,
    LessonDraft,
    LessonTarget,
    LessonValues,
    ProposalRecord,
    ProtectionEventDecided,
    ProtectionEventTarget,
    ProtectionEventValues,
    QuestionResearch,
    QuizQuestionDecided,
    QuizQuestionSuggested,
    QuizQuestionTarget,
    QuizQuestionValues,
    ThreatEventDecided,
    ThreatEventTarget,
    ThreatEventValues,
    WeightChangeTarget,
    WeightDelta,
)
from imageshield.intel.proposal_store import (
    LIVE_CREDIT_SQL,
    PROPOSAL_COLUMNS,
    PROPOSAL_WRITE_LOCK,
    fetch_linked_signals,
    load_scoring_vocabulary,
    overlap_pool,
    question_research,
    record_of,
)
from imageshield.intel.recency import Recency
from imageshield.intel.tags import TagRegistry, membership_problems
from imageshield.intel.vocabulary import ScoringVocabulary

log = structlog.get_logger("imageshield.intel")

_CELL_INDEX = "intel_proposals_one_approved_per_cell"
_RENEWED_ONCE = "protection_events_renews_event_id_key"
_ATTESTATION_REQUIRED = (
    "A protection is approved only with applies_regardless_of_location: true;"
    " one limited to some places is rejected."
)

_MESSAGES: dict[DecisionRefusal, str] = {
    "proposal_not_found": "No proposal with this id.",
    "proposal_not_pending": "This proposal is no longer open to this decision.",
    "proposal_not_decidable": "This kind of proposal cannot take this decision.",
    "proposal_evidence_retracted": "Every signal behind this proposal has been retracted.",
    "proposal_evidence_stale": (
        "Every piece of evidence behind this threat is dated and older than the recency window;"
        " an old or ended incident is not a threat."
    ),
    "proposal_uncorroborated": "Web-only evidence needs a second, independent source.",
    "proposal_tags_unmapped": "No option of the live quiz maps to any of this event's tags.",
    "proposal_cell_awaiting_publish": "Another approved change for this option awaits publish.",
    "values_out_of_bounds": "These values fall outside the bounds for this option.",
    "unknown_tag": "A tag this approval adds is not registered.",
    "tag_retired": "A retired tag cannot be added.",
    "question_incomplete": (
        "Every answer needs points from 0 to 10, and a question that takes several answers needs a"
        " cap."
    ),
    "question_key_taken": "The live quiz already has a question with this key.",
    "lesson_invalid": "This lesson breaks a rule: see problems.",
}

_RESEARCH_RUNNING = "Research for this question is still running."

_RENEWAL_ENDED = "The protection this renews has been retracted or has reached its review date."

_WHY_NOT_REFUSAL: dict[str, DecisionRefusal] = {
    "not_decidable": "proposal_not_decidable",
    "renewed_credit_ended": "proposal_not_pending",
    "evidence_retracted": "proposal_evidence_retracted",
    "evidence_stale": "proposal_evidence_stale",
    "uncorroborated": "proposal_uncorroborated",
    "tags_unmapped": "proposal_tags_unmapped",
}

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_APPROVE_SQL = """
    UPDATE intel_proposals SET status = 'approved', decided = %(decided)s,
           decided_by = %(operator)s, decided_at = now(), decision_reason = %(reason)s
     WHERE proposal_id = %(proposal_id)s AND status = 'pending'
    RETURNING status, applied_ref, decided
"""

_APPLY_EVENT_SQL = """
    UPDATE intel_proposals SET status = 'applied', decided = %(decided)s,
           applied_ref = %(applied_ref)s, decided_by = %(operator)s, decided_at = now(),
           decision_reason = %(reason)s
     WHERE proposal_id = %(proposal_id)s AND status = 'pending'
    RETURNING status, applied_ref, decided
"""

# decay_days is NOT NULL and inert since 0037: supplied as expires_in_days, never shown
# (spec §4.5). No domains and never global: an intel threat reaches people by tags alone.
_INSERT_THREAT_SQL = """
    INSERT INTO threat_events (kind, title, body, severity, tags, domains, is_global,
        expires_at, decay_days, status, created_by, proposal_id)
    VALUES (%(kind)s, %(title)s, %(body)s, %(severity)s, %(tags)s, '{}', false,
        now() + make_interval(days => %(days)s), %(days)s, 'active', %(operator)s,
        %(proposal_id)s)
    RETURNING event_id
"""

# body is the decided "what it means for you" ('' on a proposal written before 2026-10-06,
# spec 2026-10-06-intel-event-body). starts_at is now(), or for a renewal
# the old credit's review_by, so the two never overlap and never leave a gap (spec §4.7).
_INSERT_PROTECTION_SQL = """
    INSERT INTO protection_events (title, body, strength, tags, is_global, starts_at, review_by,
        status, proposal_id, renews_event_id, created_by)
    VALUES (%(title)s, %(body)s, %(strength)s, %(tags)s::text[], %(is_global)s,
        coalesce(%(starts)s::timestamptz, now()),
        coalesce(%(starts)s::timestamptz, now()) + make_interval(days => %(days)s),
        'active', %(proposal_id)s, %(renews)s::uuid, %(operator)s)
    RETURNING event_id
"""

_REJECT_SQL = """
    UPDATE intel_proposals SET status = 'rejected', decided_by = %(operator)s,
           decided_at = now(), decision_reason = %(reason)s
     WHERE proposal_id = %(proposal_id)s AND status = %(from_status)s
    RETURNING status, applied_ref, decided
"""

# The pending proposals an approval covers (intel/overlap.py). Literal statuses, as everywhere.
_COVERED_SQL = """
    UPDATE intel_proposals SET status = 'superseded', supersede_reason = 'covered_by_decision'
     WHERE proposal_id = ANY(%s::uuid[]) AND status = 'pending'
    RETURNING proposal_id
"""

_APPLIED_SQL = """
    UPDATE intel_proposals SET status = 'applied', applied_ref = %(scoring_version)s
     WHERE proposal_id = ANY(%(ids)s::uuid[]) AND kind IN ('weight_change', 'quiz_question')
       AND status = 'approved'
    RETURNING proposal_id
"""


def _known(suggested: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    """The suggested keys this release's decided model knows (final review I4, 2026-10-12). A key a
    newer release added (as card_line was) would otherwise make an older image refuse the whole
    decision as out of bounds, so a rollback could not approve proposals the newer one wrote."""
    return {k: v for k, v in suggested.items() if k in model.model_fields}


def _refuse(code: DecisionRefusal) -> DecisionRefused:
    return DecisionRefused(code, _MESSAGES[code])


def _check_lesson(raw: dict[str, Any] | None, active: Sequence[ContextSignal]) -> None:
    """A decided lesson must pass every rule against the evidence it was approved on (spec
    2026-10-15-intel-lessons-and-evidence §3): the word rules, every figure quoted, and a link only
    to a cited vetted page."""
    if raw is None:
        return
    problems = lesson_problems(LessonDraft.model_validate(raw), active)
    if problems:
        raise DecisionRefused(
            "lesson_invalid", _MESSAGES["lesson_invalid"], problems=tuple(problems)
        )


def _threat_decided(
    proposal: ProposalRecord,
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    active: Sequence[ContextSignal],
) -> dict[str, Any]:
    """The exact values a threat approval stores: the operator's ``values`` merged over
    ``suggested`` and the proposal's own ``target.tags`` (spec note 2026-09-30), re-checked
    against §4.5. A tag the edit ADDS must be registered and not retired (§3.1: unknown_tag /
    tag_retired, naming it); a tag already on the target may be retired. A final tag set that
    is entirely unmapped would create an event that reaches nobody: proposal_tags_unmapped."""
    try:
        target = ThreatEventTarget.model_validate(proposal.target)
        edit = ThreatEventValues.model_validate(values or {})
        decided = ThreatEventDecided.model_validate(
            {
                **_known(proposal.suggested, ThreatEventDecided),
                "tags": list(target.tags),
                **edit.model_dump(exclude_unset=True),
            }
        )
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    registry = (
        vocabulary.registry() if vocabulary is not None else TagRegistry(frozenset(), frozenset())
    )
    added = [t for t in decided.tags if t not in target.tags]
    unknown, retired = membership_problems(added, registry)
    if unknown:
        raise DecisionRefused("unknown_tag", _MESSAGES["unknown_tag"], slugs=tuple(unknown))
    if retired:
        raise DecisionRefused("tag_retired", _MESSAGES["tag_retired"], slugs=tuple(retired))
    if all_tags_unmapped({"tags": list(decided.tags)}, vocabulary):
        raise _refuse("proposal_tags_unmapped")
    dumped = decided.model_dump(mode="json")
    _check_lesson(dumped.get("lesson"), active)
    return dumped


async def _insert_threat_event(
    conn: AsyncConnection[Any], proposal_id: UUID, decided: dict[str, Any], operator: str
) -> UUID:
    cur = await conn.execute(
        _INSERT_THREAT_SQL,
        {
            "kind": decided["kind"],
            "title": decided["title"],
            "body": decided.get("body", ""),
            "severity": decided["severity"],
            "tags": list(decided["tags"]),
            "days": decided["expires_in_days"],
            "operator": operator,
            "proposal_id": proposal_id,
        },
    )
    row = await cur.fetchone()
    assert row is not None
    event_id: UUID = row[0]
    return event_id


def _protection_decided(
    proposal: ProposalRecord,
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    attested: bool | None,
    active: Sequence[ContextSignal],
) -> dict[str, Any]:
    """The exact values a protection approval stores (spec §3.6, §4.7 as amended 2026-09-30):
    the operator's ``values`` merged over ``suggested`` and the target's own scope, re-checked
    against §4.5, plus the location attestation, which must be true. Tags follow the threat rule
    (§3.1, the controller's ruling): a tag the edit ADDS that is unregistered is unknown_tag, one
    that is retired tag_retired, both naming it; a tag already on the target may be retired. A
    final scope of only unmapped tags would reach nobody: proposal_tags_unmapped."""
    if attested is not True:
        raise DecisionRefused("values_out_of_bounds", _ATTESTATION_REQUIRED)
    try:
        target = ProtectionEventTarget.model_validate(proposal.target)
        edit = ProtectionEventValues.model_validate(values or {})
        decided = ProtectionEventDecided.model_validate(
            {
                **_known(proposal.suggested, ProtectionEventDecided),
                "tags": list(target.tags),
                "is_global": target.is_global,
                **edit.model_dump(exclude_unset=True),
                "applies_regardless_of_location": True,
            }
        )
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    registry = (
        vocabulary.registry() if vocabulary is not None else TagRegistry(frozenset(), frozenset())
    )
    added = [t for t in decided.tags if t not in target.tags]
    unknown, retired = membership_problems(added, registry)
    if unknown:
        raise DecisionRefused("unknown_tag", _MESSAGES["unknown_tag"], slugs=tuple(unknown))
    if retired:
        raise DecisionRefused("tag_retired", _MESSAGES["tag_retired"], slugs=tuple(retired))
    if all_tags_unmapped({"tags": list(decided.tags), "is_global": decided.is_global}, vocabulary):
        raise _refuse("proposal_tags_unmapped")
    dumped = decided.model_dump(mode="json")
    _check_lesson(dumped.get("lesson"), active)
    return dumped


def _lesson_decided(
    proposal: ProposalRecord,
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    active: Sequence[ContextSignal],
) -> dict[str, Any]:
    """The exact lesson an approval stores (spec 2026-10-15-intel-lessons-and-evidence §5): the
    edit merged over ``suggested``; every tag registered and live; a general lesson reaches
    somebody (tags or global); and the lesson passes every rule against its own evidence."""
    try:
        target = LessonTarget.model_validate(proposal.target)
        edit = LessonValues.model_validate(values or {})
        decided = LessonDecided.model_validate(
            {**_known(proposal.suggested, LessonDecided), **edit.model_dump(exclude_unset=True)}
        )
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    registry = (
        vocabulary.registry() if vocabulary is not None else TagRegistry(frozenset(), frozenset())
    )
    unknown, retired = membership_problems(list(decided.tags), registry)
    if unknown:
        raise DecisionRefused("unknown_tag", _MESSAGES["unknown_tag"], slugs=tuple(unknown))
    if retired:
        raise DecisionRefused("tag_retired", _MESSAGES["tag_retired"], slugs=tuple(retired))
    if target.topic is not None and not decided.tags and not decided.is_global:
        raise _refuse("values_out_of_bounds")
    dumped = decided.model_dump(mode="json")
    _check_lesson(dumped["lesson"], active)
    return dumped


def _question_decided(
    proposal: ProposalRecord,
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    research: QuestionResearch | None,
) -> dict[str, Any]:
    """The exact question an approval stores (spec 2026-10-13-intel-quiz-questions §5): the
    operator's ``values`` merged over ``suggested``, answer by answer, each answer's points
    defaulting to the newest delivered suggestion's number for it. An answer with no number, or a
    multi-select with no cap, is question_incomplete; every tag must be registered and live; and the
    key must still be free in the live quiz."""
    try:
        target = QuizQuestionTarget.model_validate(proposal.target)
        suggested = QuizQuestionSuggested.model_validate(proposal.suggested)
        edit = QuizQuestionValues.model_validate(values or {})
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    if edit.options is not None and len(edit.options) != len(suggested.options):
        raise _refuse("values_out_of_bounds")
    numbers = research.deductions if research is not None else {}
    options: list[dict[str, Any]] = []
    for index, base in enumerate(suggested.options):
        change = edit.options[index] if edit.options is not None else None
        deduction = (
            change.deduction
            if change is not None and change.deduction is not None
            else numbers.get(base.option)
        )
        if deduction is None:
            raise _refuse("question_incomplete")
        text = change.option if change is not None and change.option is not None else base.option
        tags = change.tags if change is not None and change.tags is not None else base.tags
        options.append({"option": text, "deduction": deduction, "tags": list(tags)})
    multi = edit.multi if edit.multi is not None else suggested.multi
    cap = edit.cap if "cap" in edit.model_fields_set else None
    if multi and cap is None:
        raise _refuse("question_incomplete")
    try:
        decided = QuizQuestionDecided.model_validate(
            {
                "prompt": edit.prompt if edit.prompt is not None else suggested.prompt,
                "multi": multi,
                "cap": cap,
                "options": options,
                "suggestion_proposal_id": research.suggestion_proposal_id
                if research is not None
                else None,
            }
        )
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    registry = (
        vocabulary.registry() if vocabulary is not None else TagRegistry(frozenset(), frozenset())
    )
    every = list(dict.fromkeys(t for o in decided.options for t in o.tags))
    unknown, retired = membership_problems(every, registry)
    if unknown:
        raise DecisionRefused("unknown_tag", _MESSAGES["unknown_tag"], slugs=tuple(unknown))
    if retired:
        raise DecisionRefused("tag_retired", _MESSAGES["tag_retired"], slugs=tuple(retired))
    if vocabulary is not None and target.question_key in vocabulary.questions:
        raise _refuse("question_key_taken")
    return decided.model_dump(mode="json")


def _question_published(
    proposal: ProposalRecord, vocabulary: ScoringVocabulary | None
) -> bool:
    """The live quiz carries exactly this approved question: its key, its answers in order, and
    each answer's decided points. Like a weight change's ``published_unacknowledged``."""
    if vocabulary is None or proposal.decided is None:
        return False
    live = vocabulary.questions.get(str(proposal.target.get("question_key", "")))
    if live is None:
        return False
    try:
        decided = QuizQuestionDecided.model_validate(proposal.decided)
    except ValidationError:
        return False
    return tuple(o.option for o in decided.options) == tuple(live.options) and all(
        live.deductions.get(o.option) == o.deduction for o in decided.options
    )


async def _publish_event(
    conn: AsyncConnection[Any],
    proposal_id: UUID,
    event_id: UUID,
    kind: Literal["threat", "protection"],
    decided: dict[str, Any],
    operator: str,
    active: Sequence[ContextSignal],
) -> UUID | None:
    """An approved event's evidence snapshot, and its lesson with its own snapshot when the
    decision carries one (spec 2026-10-15-intel-lessons-and-evidence §4, §6). In the decision's
    transaction. Returns the lesson id, or None."""
    await snapshot_evidence(
        conn, owner_kind="event", owner_id=event_id, proposal_id=proposal_id, active=active
    )
    if decided.get("lesson") is None:
        return None
    lesson_id = await insert_lesson(
        conn,
        proposal_id=proposal_id,
        event_id=event_id,
        kind=kind,
        topic=None,
        decided=decided,
        operator=operator,
    )
    await snapshot_evidence(
        conn, owner_kind="lesson", owner_id=lesson_id, proposal_id=proposal_id, active=active
    )
    return lesson_id


def _renews_of(proposal: ProposalRecord) -> UUID | None:
    """The credit a protection proposal renews, or None. A target that does not parse renews
    nothing here: the approval refuses it as values_out_of_bounds."""
    if proposal.kind != "protection_event":
        return None
    try:
        return ProtectionEventTarget.model_validate(proposal.target).renews_event_id
    except ValidationError:
        return None


async def _renewed_review_by(conn: AsyncConnection[Any], event_id: UUID) -> datetime | None:
    """Where a renewal starts: the review date of the credit it continues, locked so a racing
    retraction is seen. None when that credit is no longer live -- retracted, or at or past its
    review date -- and so no longer renewable (final review M3)."""
    cur = await conn.execute(
        f"SELECT review_by FROM protection_events WHERE event_id = %s AND {LIVE_CREDIT_SQL}"
        " FOR UPDATE",
        (event_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    review_by: datetime = row[0]
    return review_by


async def _insert_protection_event(
    conn: AsyncConnection[Any],
    proposal_id: UUID,
    decided: dict[str, Any],
    operator: str,
    *,
    renews: UUID | None,
    starts: datetime | None,
) -> UUID:
    cur = await conn.execute(
        _INSERT_PROTECTION_SQL,
        {
            "title": decided["title"],
            "body": decided.get("body", ""),
            "strength": decided["strength"],
            "tags": list(decided["tags"]),
            "is_global": decided["is_global"],
            "starts": starts,
            "days": decided["review_in_days"],
            "proposal_id": proposal_id,
            "renews": renews,
            "operator": operator,
        },
    )
    row = await cur.fetchone()
    assert row is not None
    event_id: UUID = row[0]
    return event_id


def _approval_decided(
    proposal: ProposalRecord,
    active: Sequence[ContextSignal],
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    *,
    recency: Recency,
    attested: bool | None = None,
    renewed_credit_ended: bool = False,
    research: QuestionResearch | None = None,
) -> dict[str, Any]:
    """The exact values an approval stores, or a refusal."""
    if proposal.kind not in APPROVABLE_KINDS:
        raise _refuse("proposal_not_decidable")
    if proposal.status != "pending":
        raise _refuse("proposal_not_pending")
    reason = why_not(
        proposal,
        active,
        vocabulary,
        recency=recency,
        renewed_credit_ended=renewed_credit_ended,
        research_pending=research is not None and research.pending,
    )
    if reason == "renewed_credit_ended":
        raise DecisionRefused("proposal_not_pending", _RENEWAL_ENDED)
    if reason == "research_pending":
        raise DecisionRefused("proposal_not_decidable", _RESEARCH_RUNNING)
    if reason is not None:
        raise _refuse(_WHY_NOT_REFUSAL[reason])
    if proposal.kind == "quiz_question":
        return _question_decided(proposal, vocabulary, values, research)
    if proposal.kind == "lesson":
        return _lesson_decided(proposal, vocabulary, values, active)
    if proposal.kind == "threat_event":
        return _threat_decided(proposal, vocabulary, values, active)
    if proposal.kind == "protection_event":
        return _protection_decided(proposal, vocabulary, values, attested, active)
    try:
        target = WeightChangeTarget.model_validate(proposal.target)
        # MERGED over ``suggested`` (2026-10-06), as a threat's and a protection's edit is: an
        # operator who changes only the delta keeps the model's reason, and one who rewrites only
        # the reason keeps the delta (spec 2026-10-06-intel-weight-reason).
        delta = WeightDelta.model_validate({**proposal.suggested, **(values or {})})
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    if vocabulary is None or cell_problem(
        vocabulary,
        question_key=target.question_key,
        option=target.option,
        current=target.current,
        delta=delta.delta,
    ):
        raise _refuse("values_out_of_bounds")
    return delta.model_dump()


def _rejection_from(proposal: ProposalRecord, vocabulary: ScoringVocabulary | None) -> str:
    """The status a rejection moves FROM: pending, or approved for a withdrawal of an
    unapplied weight change. A change the live quiz shows as published but unacknowledged is
    never withdrawn (spec §4.9): a weight in force is never recorded as rejected."""
    if proposal.kind not in REJECTABLE_KINDS:
        raise _refuse("proposal_not_decidable")
    if proposal.status == "pending":
        return "pending"
    if proposal.status == "approved" and proposal.kind == "quiz_question":
        # Published, the acknowledgement not yet here: a question in the live quiz is never
        # recorded as withdrawn (spec 2026-10-13-intel-quiz-questions §5). "Published" is THIS
        # question live -- its answers and their points -- not merely its key: a key the quiz
        # editor took by hand leaves the approved question withdrawable (final review, 2026-10-13).
        if _question_published(proposal, vocabulary):
            raise _refuse("proposal_not_pending")
        return "approved"
    if proposal.status == "approved" and proposal.kind == "weight_change":
        if vocabulary is not None and proposal.decided is not None:
            target = WeightChangeTarget.model_validate(proposal.target)
            delta = WeightDelta.model_validate(proposal.decided)
            if published_unacknowledged(
                vocabulary,
                question_key=target.question_key,
                option=target.option,
                current=target.current,
                delta=delta.delta,
            ):
                raise _refuse("proposal_not_pending")
        return "approved"
    raise _refuse("proposal_not_pending")


class DecisionStore(Protocol):
    async def decide(
        self,
        proposal_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
        values: dict[str, Any] | None,
        reason: str,
        operator: str,
        applies_regardless_of_location: bool | None = None,
    ) -> Decided: ...
    async def mark_applied(
        self, *, scoring_version: str, proposal_ids: Sequence[UUID]
    ) -> AppliedResult: ...


class PostgresDecisionStore:
    """``threat_recency_days`` is ``INTEL_THREAT_RECENCY_DAYS`` (spec
    2026-10-04-intel-evidence-quality §3), required with no default: the decision re-checks
    ``evidence_stale`` with it at the moment it decides. ``clock`` is for tests."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        threat_recency_days: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._recency_days = threat_recency_days
        self._clock = clock or (lambda: datetime.now(UTC))

    async def decide(
        self,
        proposal_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
        values: dict[str, Any] | None,
        reason: str,
        operator: str,
        applies_regardless_of_location: bool | None = None,
    ) -> Decided:
        event_id: UUID | None = None
        lesson_id: UUID | None = None
        renews: UUID | None = None
        superseded: list[UUID] = []
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(PROPOSAL_WRITE_LOCK)
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    f"SELECT {PROPOSAL_COLUMNS} FROM intel_proposals"
                    " WHERE proposal_id = %s FOR UPDATE",
                    (proposal_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise _refuse("proposal_not_found")
                proposal = record_of(row)
                vocabulary = await load_scoring_vocabulary(conn)
                if decision == "approved":
                    # The credit a renewal continues, locked now -- still after the proposal,
                    # the retraction's order -- so the predicate below sees it as it stands.
                    renews = _renews_of(proposal)
                    starts = await _renewed_review_by(conn, renews) if renews else None
                    linked = (await fetch_linked_signals(conn, [proposal_id]))[proposal_id]
                    active = [s for s in linked if s.status == "active"]
                    research = (await question_research(conn, [row])).get(proposal_id)
                    decided = _approval_decided(
                        proposal,
                        active,
                        vocabulary,
                        values,
                        recency=Recency(self._clock(), self._recency_days),
                        attested=applies_regardless_of_location,
                        renewed_credit_ended=renews is not None and starts is None,
                        research=research,
                    )
                    from_status = "pending"
                    if proposal.kind == "threat_event":
                        event_id = await _insert_threat_event(conn, proposal_id, decided, operator)
                        lesson_id = await _publish_event(
                            conn, proposal_id, event_id, "threat", decided, operator, active
                        )
                        await cur.execute(
                            _APPLY_EVENT_SQL,
                            {
                                "proposal_id": proposal_id,
                                "decided": Jsonb(decided),
                                "applied_ref": str(event_id),
                                "operator": operator,
                                "reason": reason,
                            },
                        )
                    elif proposal.kind == "protection_event":
                        event_id = await _insert_protection_event(
                            conn, proposal_id, decided, operator, renews=renews, starts=starts
                        )
                        lesson_id = await _publish_event(
                            conn, proposal_id, event_id, "protection", decided, operator, active
                        )
                        if (
                            lesson_id is None
                            and renews is not None
                            and "lesson" not in (values or {})
                        ):
                            # A renewal keeps the lesson it continues unless the decision named
                            # one, a null included (spec 2026-10-15 §4).
                            lesson_id = await carry_lesson(
                                conn,
                                from_event_id=renews,
                                to_event_id=event_id,
                                proposal_id=proposal_id,
                                operator=operator,
                            )
                            if lesson_id is not None:
                                await snapshot_evidence(
                                    conn,
                                    owner_kind="lesson",
                                    owner_id=lesson_id,
                                    proposal_id=proposal_id,
                                    active=active,
                                )
                        await cur.execute(
                            _APPLY_EVENT_SQL,
                            {
                                "proposal_id": proposal_id,
                                "decided": Jsonb(decided),
                                "applied_ref": str(event_id),
                                "operator": operator,
                                "reason": reason,
                            },
                        )
                    elif proposal.kind == "lesson":
                        lesson_target = LessonTarget.model_validate(proposal.target)
                        kind = (
                            "general"
                            if lesson_target.event_id is None
                            else await event_kind(conn, lesson_target.event_id)
                        )
                        if kind is None:
                            raise _refuse("proposal_not_pending")  # its event is gone
                        lesson_id = await insert_lesson(
                            conn,
                            proposal_id=proposal_id,
                            event_id=lesson_target.event_id,
                            kind=kind,
                            topic=lesson_target.topic,
                            decided=decided,
                            operator=operator,
                        )
                        await snapshot_evidence(
                            conn,
                            owner_kind="lesson",
                            owner_id=lesson_id,
                            proposal_id=proposal_id,
                            active=active,
                        )
                        await cur.execute(
                            _APPLY_EVENT_SQL,
                            {
                                "proposal_id": proposal_id,
                                "decided": Jsonb(decided),
                                "applied_ref": str(lesson_id),
                                "operator": operator,
                                "reason": reason,
                            },
                        )
                    else:
                        await cur.execute(
                            _APPROVE_SQL,
                            {
                                "proposal_id": proposal_id,
                                "decided": Jsonb(decided),
                                "operator": operator,
                                "reason": reason,
                            },
                        )
                else:
                    from_status = _rejection_from(proposal, vocabulary)
                    await cur.execute(
                        _REJECT_SQL,
                        {
                            "proposal_id": proposal_id,
                            "from_status": from_status,
                            "operator": operator,
                            "reason": reason,
                        },
                    )
                updated = await cur.fetchone()
                if updated is None:  # belt and braces: the row is locked
                    raise _refuse("proposal_not_pending")
                if decision == "approved":
                    # What both reads showed as ``overlaps``, computed from the proposal as it
                    # stood (pending) against every pending proposal now.
                    pool = await overlap_pool(conn, vocabulary, extra=[row])
                    covered = [
                        o["proposal_id"]
                        for o in overlaps_of(pool, only=[proposal_id]).get(proposal_id, [])
                    ]
                    if covered:
                        moved = await conn.execute(_COVERED_SQL, (covered,))
                        superseded = [r[0] for r in await moved.fetchall()]
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "operator",
                        "action": "intel.proposal_decided",
                        "resource_id": proposal_id,
                        "metadata": Jsonb(
                            {
                                "operator": operator,
                                "kind": proposal.kind,
                                "decision": decision,
                                "from_status": from_status,
                                "decided": updated["decided"],
                                "reason": reason,
                                **({"event_id": str(event_id)} if event_id is not None else {}),
                                **(
                                    {"lesson_id": str(lesson_id)}
                                    if lesson_id is not None
                                    else {}
                                ),
                                **(
                                    {"superseded": [str(i) for i in superseded]}
                                    if superseded
                                    else {}
                                ),
                                **(
                                    {
                                        "applies_regardless_of_location": True,
                                        "renews_event_id": str(renews) if renews else None,
                                    }
                                    if decision == "approved"
                                    and proposal.kind == "protection_event"
                                    else {}
                                ),
                            }
                        ),
                    },
                )
        except UniqueViolation as exc:
            if exc.diag.constraint_name == _CELL_INDEX:
                raise _refuse("proposal_cell_awaiting_publish") from exc
            if exc.diag.constraint_name == _RENEWED_ONCE:
                raise DecisionRefused(
                    "proposal_not_pending", "This protection has already been renewed."
                ) from exc
            raise
        return Decided(
            proposal_id=proposal_id,
            kind=proposal.kind,
            status=updated["status"],
            applied_ref=updated["applied_ref"],
            decided=updated["decided"],
            superseded=tuple(superseded),
        )

    async def mark_applied(
        self, *, scoring_version: str, proposal_ids: Sequence[UUID]
    ) -> AppliedResult:
        ids = list(dict.fromkeys(proposal_ids))
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_APPLIED_SQL, {"scoring_version": scoring_version, "ids": ids})
            moved = [r[0] for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT proposal_id FROM intel_proposals WHERE proposal_id = ANY(%s::uuid[])"
                " AND kind IN ('weight_change', 'quiz_question') AND status = 'applied'",
                (ids,),
            )
            applied_now = {r[0] for r in await cur.fetchall()}
            if moved:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.proposals_applied",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "scoring_version": scoring_version,
                                "proposal_ids": [str(i) for i in moved],
                            }
                        ),
                    },
                )
        moved_set = set(moved)
        return AppliedResult(
            applied=tuple(moved),
            already_applied=tuple(i for i in ids if i in applied_now and i not in moved_set),
            not_applied=tuple(i for i in ids if i not in applied_now),
        )
