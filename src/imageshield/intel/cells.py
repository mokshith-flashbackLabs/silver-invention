"""The weight cell -- one option of one question (spec §4.5, §4.9).

``cell_problem`` is §4.5's weight_change rule. Generation runs it before writing, and the
decision runs it again on the operator's final delta. ``stale_reason`` and
``published_unacknowledged`` are §4.9's reads of an approved change the quiz has moved under.
One module, so the four callers cannot disagree about what a live cell is.
"""

from __future__ import annotations

from typing import Literal

from imageshield.intel.bounds import (
    DEDUCTION_MAX,
    DEDUCTION_MIN,
    WEIGHT_DELTA_MAX,
    WEIGHT_DELTA_MIN,
)
from imageshield.intel.vocabulary import ScoringVocabulary

CellProblem = Literal[
    "unknown_question",
    "not_mutable",
    "unknown_option",
    "current_mismatch",
    "delta_out_of_bounds",
    "result_out_of_bounds",
]
StaleReason = Literal["option_renamed", "cell_removed", "not_mutable", "deduction_moved"]


def cell_problem(
    vocabulary: ScoringVocabulary, *, question_key: str, option: str, current: int, delta: int
) -> CellProblem | None:
    question = vocabulary.question(question_key)
    if question is None:
        return "unknown_question"
    if not question.mutable:
        return "not_mutable"
    live = vocabulary.deduction(question_key, option)
    if live is None:
        return "unknown_option"
    if live != current:
        return "current_mismatch"
    if delta == 0 or not WEIGHT_DELTA_MIN <= delta <= WEIGHT_DELTA_MAX:
        return "delta_out_of_bounds"
    result = current + delta
    if not DEDUCTION_MIN <= result <= DEDUCTION_MAX or (
        question.cap is not None and result > question.cap
    ):
        return "result_out_of_bounds"
    return None


def stale_reason(
    vocabulary: ScoringVocabulary,
    *,
    question_key: str,
    option: str,
    current: int,
    generated_at_release: int,
) -> StaleReason | None:
    """Why a proposal's cell no longer matches the live quiz, or None. ``option_renamed``
    only when the rename log moved this very option after the proposal was generated;
    otherwise a missing option is ``cell_removed``."""
    question = vocabulary.question(question_key)
    if question is None:
        return "cell_removed"
    live = vocabulary.deduction(question_key, option)
    if live is None:
        if vocabulary.renamed_away(question_key, option, after_release_no=generated_at_release):
            return "option_renamed"
        return "cell_removed"
    if not question.mutable:
        return "not_mutable"
    if live != current:
        return "deduction_moved"
    return None


def published_unacknowledged(
    vocabulary: ScoringVocabulary, *, question_key: str, option: str, current: int, delta: int
) -> bool:
    """spec §4.9: the live deduction equals ``current + decided.delta`` exactly, so the change
    was published and its acknowledgement has not landed yet. Never stale; never withdrawable."""
    return vocabulary.deduction(question_key, option) == current + delta
