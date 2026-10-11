"""Search volume and research limits (spec 2026-10-10-intel-every-answer-researched §5.1).

Every value is a required IntelConfig key with no code default (owner, 2026-10-10: "do not
hardcode anything"); this is the one value the run code reads them through. Safety limits stay
code constants in ``intel/bounds.py``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchLimits:
    # Stage 1 (source proposal): candidates kept per answer, and of those, saved searches.
    proposed_sources_per_answer: int
    proposed_searches_per_answer: int
    # A saved-search read keeps at least / at most this many of its searches' result pages.
    discovery_min_result_pages: int
    discovery_max_result_pages: int
    # Stage 3: a validation search's searches, and the result pages fetched before it counts as
    # empty.
    validation_searches: int
    validation_result_pages: int
    # Stage 1: extra asks for answers no source is aimed at (0: off).
    source_proposal_coverage_asks: int
    # Stage 4: research rounds per suggestion run (0: off), and searches per unbacked answer
    # per round.
    suggestion_research_rounds: int
    research_searches_per_answer: int
