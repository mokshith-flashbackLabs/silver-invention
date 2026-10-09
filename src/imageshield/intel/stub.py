"""The development model (spec §4.1): reads nothing, proposes nothing, costs
nothing, and says so. Built INSTEAD of the Claude client, so no object in a dev
process holds a live client. Not a fixture generator.
"""

from __future__ import annotations

from decimal import Decimal

from imageshield.intel.model import ModelCall
from imageshield.intel.pricing import Usage
from imageshield.intel.schemas import (
    DiscoveryOutput,
    ExtractionOutput,
    ProposalOutput,
    SourceProposalOutput,
    SuggestionOutput,
)

_ZERO = Usage(0, 0, 0, 0, 0)


class StubIntelModel:
    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]:
        return ModelCall(
            ExtractionOutput(signals=[]), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0
        )

    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return ModelCall(
            DiscoveryOutput(candidates=[]), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0
        )

    async def propose(self, system: str, user: str) -> ModelCall[ProposalOutput]:
        return ModelCall(ProposalOutput(), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0)

    async def propose_sources(self, system: str, user: str) -> ModelCall[SourceProposalOutput]:
        return ModelCall(SourceProposalOutput(), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0)

    async def choose_results(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return ModelCall(
            DiscoveryOutput(candidates=[]), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0
        )

    async def search_once(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return ModelCall(
            DiscoveryOutput(candidates=[]), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0
        )

    async def suggest_weights(self, system: str, user: str) -> ModelCall[SuggestionOutput]:
        return ModelCall(SuggestionOutput(), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0)
