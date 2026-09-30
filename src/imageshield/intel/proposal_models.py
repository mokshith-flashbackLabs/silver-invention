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

from pydantic import BaseModel, ConfigDict, Field, StrictInt

SupersedeReason = Literal["newer_proposal", "cell_changed", "resolved_by_quiz"]
TagKind = Literal["platform", "service", "practice"]


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeightChangeTarget(_Stored):
    question_key: str = Field(min_length=1)
    option: str = Field(min_length=1)
    current: StrictInt


class WeightDelta(_Stored):
    """A weight_change's ``suggested`` and ``decided``. StrictInt, so a fractional value or
    a boolean is refused rather than coerced."""

    delta: StrictInt


class SuggestedTag(_Stored):
    slug: str
    label: str
    kind: TagKind


class CoverageGapTarget(_Stored):
    subject: str = Field(min_length=1)
    suggested_tag: SuggestedTag | None = None
    suggested_question: str | None = None
    regenerated_by_run_id: UUID | None = None


@dataclass(frozen=True)
class ContextSignal:
    """An active-or-retracted signal with the provenance the predicates need. ``trust`` and
    ``publisher_domain`` come from its document."""

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


@dataclass(frozen=True)
class NewProposal:
    """One validated proposal, pre-insert. Step 3 widens ``kind``."""

    kind: Literal["weight_change", "coverage_gap"]
    target: dict[str, Any]
    suggested: dict[str, Any]
    rationale: str
    signal_ids: tuple[UUID, ...]


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
class WriteResult:
    written: tuple[UUID, ...]
    superseded: tuple[UUID, ...]


@dataclass(frozen=True)
class Decided:
    proposal_id: UUID
    kind: str
    status: str
    applied_ref: str | None
    decided: dict[str, Any] | None


DecisionRefusal = Literal[
    "proposal_not_found",
    "proposal_not_pending",
    "proposal_not_decidable",
    "proposal_evidence_retracted",
    "proposal_uncorroborated",
    "proposal_tags_unmapped",
    "proposal_cell_awaiting_publish",
    "values_out_of_bounds",
]


class DecisionRefused(Exception):
    """A decision the transaction refused. ``code`` is the §4.7 error code, verbatim."""

    def __init__(self, code: DecisionRefusal, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
