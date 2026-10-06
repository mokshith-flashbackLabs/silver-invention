"""Shapes of intel_proposals (spec §3.6), and the value types the proposal modules share.

``target`` says what a proposal is about; ``suggested`` holds the model's numbers and text,
kept forever; ``decided`` holds the exact values a named operator approved, the only field
anything downstream applies. The models here are the per-kind validation §3.6 names. They
are ``extra='forbid'``, so "an edit may change only delta" is a parse failure, not a
convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from imageshield.intel.bounds import (
    MAX_EVENT_BODY_CHARS,
    MAX_EVENT_TITLE_CHARS,
    MAX_WEIGHT_REASON_CHARS,
    PROTECTION_REVIEW_MAX_DAYS,
    PROTECTION_REVIEW_MIN_DAYS,
    PROTECTION_STRENGTH_MAX,
    PROTECTION_STRENGTH_MIN,
    THREAT_EXPIRES_MAX_DAYS,
    THREAT_EXPIRES_MIN_DAYS,
    THREAT_SEVERITY_MAX,
    THREAT_SEVERITY_MIN,
)
from imageshield.intel.tags import is_well_formed

# ``covered_by_decision`` (2026-10-04, migration 0048): an operator approved a proposal this one
# overlaps -- the same body of evidence on an overlapping tag (intel/overlap.py).
SupersedeReason = Literal[
    "newer_proposal", "cell_changed", "resolved_by_quiz", "covered_by_decision"
]
TagKind = Literal["platform", "service", "practice"]
ThreatKind = Literal["leak", "deepfake_wave", "platform_incident", "other"]


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeightChangeTarget(_Stored):
    question_key: str = Field(min_length=1)
    option: str = Field(min_length=1)
    current: StrictInt


class WeightDelta(_Stored):
    """A weight_change's ``suggested`` and ``decided``. StrictInt, so a fractional value or
    a boolean is refused rather than coerced.

    ``body`` (spec 2026-10-06-intel-weight-reason) is the change's reason in one plain sentence
    that names no platform, answer or person: the person reads it as the whole line in their score
    history when the published change moves their score. Empty on a proposal written before it."""

    delta: StrictInt
    body: str = Field(default="", max_length=MAX_WEIGHT_REASON_CHARS)

    @field_validator("body")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()


class SuggestedTag(_Stored):
    slug: str
    label: str
    kind: TagKind


class CoverageGapTarget(_Stored):
    subject: str = Field(min_length=1)
    suggested_tag: SuggestedTag | None = None
    suggested_question: str | None = None
    regenerated_by_run_id: UUID | None = None


def _distinct_slugs(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(not is_well_formed(t) for t in value) or len(set(value)) != len(value):
        raise ValueError("tags must be distinct slugs matching ^[a-z][a-z0-9_]{0,39}$")
    return value


class ThreatEventTarget(_Stored):
    """What a threat_event is about: exposure tags, never a person (spec §3.6)."""

    tags: tuple[str, ...] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _slugs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _distinct_slugs(value)


class ThreatEventSuggested(_Stored):
    """A threat_event's ``suggested``: the model's numbers and text, kept forever (§3.6). The
    bounds are §4.5's, and they hold for an operator's final values too (ThreatEventDecided).

    ``body`` is what the event means for the people it reaches, in plain words, shown in their
    app's score history beside the title (spec 2026-10-06-intel-event-body; it replaced the
    2026-09-30 note "a threat proposal carries only a title"). Empty on every proposal written
    before it, and on one whose model text was left out, so an approval still works without."""

    kind: ThreatKind
    title: str = Field(min_length=1, max_length=MAX_EVENT_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_EVENT_BODY_CHARS)
    severity: StrictInt = Field(ge=THREAT_SEVERITY_MIN, le=THREAT_SEVERITY_MAX)
    expires_in_days: StrictInt = Field(ge=THREAT_EXPIRES_MIN_DAYS, le=THREAT_EXPIRES_MAX_DAYS)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("body")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()


class ThreatEventDecided(ThreatEventSuggested):
    """A threat_event's ``decided``: the exact values an approval stores, and the only values
    the event row is inserted from -- the suggested keys plus the tags (§3.6)."""

    tags: tuple[str, ...] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _slugs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _distinct_slugs(value)


class ThreatEventValues(_Stored):
    """An operator's edit of a threat approval: any subset of the decided keys, and nothing else.
    It is merged over the proposal's ``suggested`` and its own ``target.tags``, and the result
    must parse as ThreatEventDecided. So a partial edit changes only what it names, and a
    complete one is the whole decided set (spec note 2026-09-30). Types only here; the bounds
    are ThreatEventDecided's."""

    kind: ThreatKind | None = None
    title: str | None = None
    body: str | None = None
    severity: StrictInt | None = None
    expires_in_days: StrictInt | None = None
    tags: tuple[str, ...] | None = None


def _one_scope(tags: tuple[str, ...], is_global: bool) -> None:
    if is_global == bool(tags):
        raise ValueError("a protection is scoped by tags or is global, exactly one")


class ProtectionEventTarget(_Stored):
    """What a protection_event is about (spec §3.6): exposure tags or everyone, never both and
    never neither. ``renews_event_id`` is set only on a renewal, which code writes (§4.8). A
    generated proposal is never global (§4.5): a global credit exists only by an operator's
    edit, or by renewing one."""

    tags: tuple[str, ...] = ()
    is_global: StrictBool = False
    renews_event_id: UUID | None = None

    @field_validator("tags")
    @classmethod
    def _slugs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _distinct_slugs(value)

    @model_validator(mode="after")
    def _scope(self) -> ProtectionEventTarget:
        _one_scope(self.tags, self.is_global)
        return self


class ProtectionEventSuggested(_Stored):
    """A protection_event's ``suggested``: the model's numbers and text, or on a renewal the
    prior decided values (§4.8). The bounds are §4.5's and hold for an operator's final values
    too (ProtectionEventDecided). ``body`` is what it means for the people it reaches, as a
    threat's is (spec 2026-10-06-intel-event-body); a renewal carries the prior one forward."""

    title: str = Field(min_length=1, max_length=MAX_EVENT_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_EVENT_BODY_CHARS)
    strength: StrictInt = Field(ge=PROTECTION_STRENGTH_MIN, le=PROTECTION_STRENGTH_MAX)
    review_in_days: StrictInt = Field(ge=PROTECTION_REVIEW_MIN_DAYS, le=PROTECTION_REVIEW_MAX_DAYS)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("body")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()


class ProtectionEventDecided(ProtectionEventSuggested):
    """The exact values an approval stores and the credit is inserted from (spec §3.6): the
    suggested keys, the final scope, and the operator's location attestation. It is always
    true: a protection limited to some places is rejected, never approved (§3.7)."""

    tags: tuple[str, ...] = ()
    is_global: StrictBool
    applies_regardless_of_location: Literal[True]

    @field_validator("tags")
    @classmethod
    def _slugs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _distinct_slugs(value)

    @model_validator(mode="after")
    def _scope(self) -> ProtectionEventDecided:
        _one_scope(self.tags, self.is_global)
        return self


class ProtectionEventValues(_Stored):
    """An operator's edit of a protection approval: any subset of ``{title, body, strength,
    review_in_days, tags, is_global}``, and nothing else. Merged over ``suggested`` and the
    target's scope; the result must parse as ProtectionEventDecided. So making a credit global
    names both halves of the scope, ``{is_global: true, tags: []}`` (spec note 2026-09-30).
    Types only here; the bounds are ProtectionEventDecided's."""

    title: str | None = None
    body: str | None = None
    strength: StrictInt | None = None
    review_in_days: StrictInt | None = None
    tags: tuple[str, ...] | None = None
    is_global: StrictBool | None = None


@dataclass(frozen=True)
class ContextSignal:
    """An active-or-retracted signal with the provenance the predicates need. ``trust`` and
    ``publisher_domain`` come from its document. ``document_key`` is its document's canonical
    URL hash (``intel_documents.url_hash``): what duplicate detection compares, so a page read
    again by a later run is the same document (spec §4.3, note 2026-09-30). None only where a
    test builds one by hand.

    *2026-10-04 (spec 2026-10-04-intel-evidence-quality):* ``published_at`` is its document's
    publication date (None: undated), which the recency predicate and the prompt read; ``excerpts``
    are its verbatim quotes, which corroboration compares to collapse copies of one source."""

    signal_id: UUID
    category: str
    direction: str
    tags: tuple[str, ...]
    unregistered_subjects: tuple[str, ...]
    summary: str
    trust: Literal["listed", "web"]
    publisher_domain: str
    status: str
    created_at: datetime
    document_key: str | None = None
    published_at: datetime | None = None
    excerpts: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewProposal:
    """One validated proposal, pre-insert. ``document_keys`` are the canonical URL hashes of
    its cited signals' documents: duplicate detection compares them (spec §4.3).

    ``fresh_signal_ids`` (2026-10-04) are the cited signals that are the run's own new evidence:
    what the write attaches to a pending proposal it finds this one duplicates, under its lock
    (intel/proposal_store.py), when the generation-time check could not see that proposal."""

    kind: Literal["weight_change", "coverage_gap", "threat_event", "protection_event"]
    target: dict[str, Any]
    suggested: dict[str, Any]
    rationale: str
    signal_ids: tuple[UUID, ...]
    document_keys: tuple[str, ...] = ()
    fresh_signal_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: UUID
    kind: str
    status: str
    target: dict[str, Any]
    suggested: dict[str, Any]
    decided: dict[str, Any] | None
    against_release_no: int | None
    created_at: datetime


@dataclass(frozen=True)
class PendingEvent:
    """A pending event proposal as generation reads it: for the prompt, for attach
    validation, and for duplicate detection (same kind and tag set, a shared document --
    ``document_keys`` are its signals' documents' canonical URL hashes). Every evidence field
    is over its ACTIVE signals only; ``categories`` are theirs, for the attach floor."""

    proposal_id: UUID
    kind: str
    tags: tuple[str, ...]
    title: str
    severity: int | None
    signal_ids: tuple[UUID, ...]
    document_keys: frozenset[str]
    categories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PendingWeightChange:
    """A pending weight_change as the generation prompt shows it (spec
    2026-10-04-intel-evidence-quality §5): so the model can see that evidence already backs a
    lasting change before it frames the same facts as an incident or a protection."""

    proposal_id: UUID
    question_key: str
    option: str
    delta: int | None
    signal_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class LiveEvent:
    """An active threat event that carries tags: the prompt's live_events and the detail
    read's related_events (spec §4.3, §4.7)."""

    event_id: UUID
    kind: str
    title: str
    severity: int
    tags: tuple[str, ...]
    is_global: bool
    starts_at: datetime
    expires_at: datetime
    proposal_id: UUID | None
    signal_ids: tuple[UUID, ...]

    def related(self) -> dict[str, Any]:
        """One ``related_events`` item of the detail read (Cross-repo contract, step 3)."""
        return {
            "event_id": self.event_id,
            "direction": "threat",
            "kind": self.kind,
            "title": self.title,
            "severity": self.severity,
            "tags": list(self.tags),
            "is_global": self.is_global,
            "starts_at": self.starts_at,
            "expires_at": self.expires_at,
            "proposal_id": self.proposal_id,
        }


@dataclass(frozen=True)
class LiveProtection:
    """A live protection credit: the prompt's live_protections and a protection proposal's
    related_events (spec §4.3, §4.7)."""

    event_id: UUID
    title: str
    strength: int
    tags: tuple[str, ...]
    is_global: bool
    starts_at: datetime
    review_by: datetime
    proposal_id: UUID
    renews_event_id: UUID | None
    signal_ids: tuple[UUID, ...]

    def related(self) -> dict[str, Any]:
        """One ``related_events`` item of a protection proposal's detail read."""
        return {
            "event_id": self.event_id,
            "direction": "protection",
            "kind": "protection",
            "title": self.title,
            "strength": self.strength,
            "tags": list(self.tags),
            "is_global": self.is_global,
            "starts_at": self.starts_at,
            "review_by": self.review_by,
            "proposal_id": self.proposal_id,
        }


@dataclass(frozen=True)
class Attachment:
    """New evidence for a still-pending event proposal (spec §4.3 ``attach``). It never
    changes the proposal's target, suggested or rationale."""

    proposal_id: UUID
    signal_ids: tuple[UUID, ...]


class GapRegenerateRequest(_Stored):
    """``intel_runs.request`` of a gap_regenerate run (spec §3.4, §4.9). Never person data."""

    coverage_gap_id: UUID
    tag: str
    signal_ids: tuple[UUID, ...]


class RenewalRequest(_Stored):
    """``intel_runs.request`` of a renewal_check run (spec §3.4, §4.8). Never person data."""

    event_id: UUID


@dataclass(frozen=True)
class WriteResult:
    """``converted`` / ``dropped_duplicate`` (2026-10-04): event proposals the write found to
    duplicate a pending proposal under its lock, made attachments of their new evidence or
    dropped for having none."""

    written: tuple[UUID, ...]
    superseded: tuple[UUID, ...]
    attached: tuple[UUID, ...] = ()
    attach_dropped: int = 0
    converted: int = 0
    dropped_duplicate: int = 0


@dataclass(frozen=True)
class Decided:
    """``superseded`` (2026-10-04): the pending proposals an approval superseded
    ``covered_by_decision``, because they rested on the same evidence (intel/overlap.py)."""

    proposal_id: UUID
    kind: str
    status: str
    applied_ref: str | None
    decided: dict[str, Any] | None
    superseded: tuple[UUID, ...] = ()


DecisionRefusal = Literal[
    "proposal_not_found",
    "proposal_not_pending",
    "proposal_not_decidable",
    "proposal_evidence_retracted",
    "proposal_evidence_stale",
    "proposal_uncorroborated",
    "proposal_tags_unmapped",
    "proposal_cell_awaiting_publish",
    "values_out_of_bounds",
    "unknown_tag",
    "tag_retired",
]


class DecisionRefused(Exception):
    """A decision the transaction refused. ``code`` is the §4.7 error code, verbatim, and
    ``slugs`` names the offending tags of ``unknown_tag`` / ``tag_retired`` (§3.1)."""

    def __init__(self, code: DecisionRefusal, message: str, *, slugs: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.slugs = slugs


@dataclass(frozen=True)
class AppliedResult:
    applied: tuple[UUID, ...]
    already_applied: tuple[UUID, ...]
    not_applied: tuple[UUID, ...]


@dataclass(frozen=True)
class ReconcileResult:
    release_no: int
    map_version: int
    retargeted: int
    superseded: int


@dataclass(frozen=True)
class GapPass:
    """One gap pass of the reconcile (spec §4.9): gaps resolved, regenerations queued again."""

    resolved: int
    retried: int
