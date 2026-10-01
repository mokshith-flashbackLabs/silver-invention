"""Fakes and seeds for the step-5 question runs: source proposal, validation, weight suggestion.

A module of its own rather than more of ``intel_fakes.py``: step 3 appends to that file too, and
two branches appending at one file's end is a merge conflict nobody needs."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from imageshield.intel.model import ModelCall, ModelUnavailable
from imageshield.intel.pricing import Usage
from imageshield.intel.schemas import DiscoveryOutput, SourceProposalOutput, SuggestionOutput
from tests.intel_fakes import FakeModel


class FakeQuestionModel(FakeModel):
    """``FakeModel`` plus the three step-5 calls. ``sources`` answers ``propose_sources``;
    ``searches`` maps a query to what ``search_once`` finds (anything else finds nothing);
    ``suggest_with`` receives the parsed suggestion payload, so a test can cite what the run
    retrieved. FakeModel's ``unavailable`` also fails the two web-search calls, and
    ``suggest_unavailable`` fails the suggestion."""

    def __init__(
        self,
        *args: Any,
        sources: SourceProposalOutput | None = None,
        sources_outcome: str = "ok",
        searches: dict[str, DiscoveryOutput] | None = None,
        suggest_with: Callable[[dict[str, Any]], SuggestionOutput] | None = None,
        suggestion_outcome: str = "ok",
        suggest_unavailable: ModelUnavailable | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.sources = sources
        self.sources_outcome = sources_outcome
        self.searches = searches or {}
        self.suggest_with = suggest_with
        self.suggestion_outcome = suggestion_outcome
        self.suggest_unavailable = suggest_unavailable
        self.source_proposal_calls = 0
        self.source_proposal_users: list[str] = []
        self.search_calls = 0
        self.search_users: list[str] = []
        self.suggest_calls = 0
        self.suggestion_users: list[str] = []

    async def propose_sources(self, system: str, user: str) -> ModelCall[SourceProposalOutput]:
        self.source_proposal_calls += 1
        self.source_proposal_users.append(user)
        if self.unavailable is not None:
            raise self.unavailable
        ok = self.sources_outcome == "ok"
        stop = (
            self.sources_outcome
            if self.sources_outcome in ("refusal", "max_tokens")
            else "end_turn"
        )
        return ModelCall(
            (self.sources or SourceProposalOutput()) if ok else None,
            self.sources_outcome,  # type: ignore[arg-type]
            "claude-sonnet-5",
            stop,
            Usage(1, 1, 0, 0, 1),
            Decimal("0.02"),
            1,
        )

    async def search_once(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        self.search_calls += 1
        self.search_users.append(user)
        if self.unavailable is not None:
            raise self.unavailable
        found = self.searches.get(json.loads(user)["query"], DiscoveryOutput(candidates=[]))
        return ModelCall(
            found, "ok", "claude-sonnet-5", "end_turn", Usage(1, 1, 0, 0, 1), Decimal("0.02"), 1
        )

    async def suggest_weights(self, system: str, user: str) -> ModelCall[SuggestionOutput]:
        self.suggest_calls += 1
        self.suggestion_users.append(user)
        if self.suggest_unavailable is not None:
            raise self.suggest_unavailable
        output: SuggestionOutput | None = None
        if self.suggestion_outcome == "ok":
            output = (
                self.suggest_with(json.loads(user)) if self.suggest_with else SuggestionOutput()
            )
        stop = (
            self.suggestion_outcome
            if self.suggestion_outcome in ("refusal", "max_tokens")
            else "end_turn"
        )
        return ModelCall(
            output,
            self.suggestion_outcome,  # type: ignore[arg-type]
            "claude-opus-5-5",
            stop,
            Usage(1, 1, 0, 0, 0),
            Decimal("0.05"),
            1,
        )


# The question a quiz editor asks about in these tests: a live option (Instagram, mapped to
# `instagram` in QUIZ_VOCABULARY) and a draft one (Bumble) with no tag yet.
QUESTION: dict[str, Any] = {
    "question_key": "platforms",
    "prompt": "Where do you post photos of yourself?",
    "options": ["Instagram", "Bumble"],
    "tags": {"Instagram": ["instagram"]},
}
