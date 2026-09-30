"""The model's structured output shapes (spec §4.4).

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
