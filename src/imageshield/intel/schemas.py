"""The model's structured output shapes (spec §4.4) -- extraction, discovery and,
from step 2, proposal generation.

Every model here is ``extra='forbid'``, and ``category``/``direction`` are the exact
Literals migration 0039's CHECKs enforce on ``intel_signals`` -- a model that let a
fourth value through would round-trip fine here and fail only on INSERT, several
modules away from the mistake.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedSignal(_Out):
    category: Literal["policy", "incident", "tooling", "protection", "law", "research"]
    direction: Literal["risk_up", "risk_down", "neutral"]
    tags: list[str] = Field(default_factory=list)
    unregistered_subjects: list[str] = Field(default_factory=list)
    summary: str = Field(max_length=500)
    quotes: list[str] = Field(default_factory=list)


class ExtractionOutput(_Out):
    signals: list[ExtractedSignal]


class DiscoveryCandidate(_Out):
    url: str
    reason: str


class DiscoveryOutput(_Out):
    candidates: list[DiscoveryCandidate]


class ProposedWeightChange(_Out):
    """No numeric bounds here, deliberately (spec §4.5): structured output does not enforce
    them and the SDK would validate them client-side, so one bad delta would make the whole
    response unparseable. intel/generation.py drops the one bad proposal instead."""

    question_key: str
    option: str
    current: int
    delta: int
    rationale: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposedTag(_Out):
    slug: str
    label: str
    kind: Literal["platform", "service", "practice"]


class ProposedCoverageGap(_Out):
    subject: str
    suggested_tag: ProposedTag | None = None
    suggested_question: str | None = None
    rationale: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposedThreatEvent(_Out):
    """No numeric bounds here either (see ProposedWeightChange): §4.5's severity and expiry
    bounds run per proposal in intel/generation.py. ``kind`` is an enum, which structured
    output enforces; it is not a bound the SDK checks client-side. There is no ``is_global``:
    a global threat stays hand-created (§4.5)."""

    kind: Literal["leak", "deepfake_wave", "platform_incident", "other"]
    title: str
    severity: int
    expires_in_days: int
    tags: list[str] = Field(default_factory=list)
    rationale: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposedAttach(_Out):
    proposal_id: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposedProtectionEvent(_Out):
    """No numeric bounds here either (see ProposedWeightChange): §4.5's strength and review
    bounds run per proposal in intel/generation.py. ``is_global`` is here so a model that
    believes a protection covers everyone SAYS so, and code drops that proposal
    (global_not_proposable, §4.5) rather than never hearing it: a global credit exists only by
    an operator's edit on approval."""

    title: str
    strength: int
    review_in_days: int
    tags: list[str] = Field(default_factory=list)
    is_global: bool = False
    rationale: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposalOutput(_Out):
    """Step 2's kinds as two typed lists rather than one list keyed by ``kind``, so each
    kind's shape is closed. Step 3 adds threat_events and attach; step 4 adds
    protection_events."""

    weight_changes: list[ProposedWeightChange] = Field(default_factory=list)
    coverage_gaps: list[ProposedCoverageGap] = Field(default_factory=list)
    threat_events: list[ProposedThreatEvent] = Field(default_factory=list)
    attach: list[ProposedAttach] = Field(default_factory=list)
    protection_events: list[ProposedProtectionEvent] = Field(default_factory=list)
