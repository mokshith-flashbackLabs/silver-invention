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


class ProposalOutput(_Out):
    """Step 2's kinds as two typed lists rather than one list keyed by ``kind``, so each
    kind's shape is closed. Steps 3 and 4 add threat_events, protection_events and attach."""

    weight_changes: list[ProposedWeightChange] = Field(default_factory=list)
    coverage_gaps: list[ProposedCoverageGap] = Field(default_factory=list)
