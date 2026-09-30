"""The parsed vocabulary and the weight cell (spec §3.8, §4.5, §4.9). Pure: no database."""

from __future__ import annotations

import copy

import pytest

from imageshield.intel.cells import cell_problem, published_unacknowledged, stale_reason
from imageshield.intel.models import Vocabulary
from imageshield.intel.vocabulary import normalise_subject, parse_vocabulary
from tests.intel_fakes import QUIZ_VOCABULARY, VOCABULARY, quiz_document, scoring


def _renamed(entries: list[tuple[int, str, str]]) -> list[dict[str, object]]:
    return [
        {"release_no": r, "question_key": "platforms", "old_option": old, "new_option": new}
        for r, old, new in entries
    ]


def test_parse_reads_questions_registry_map_and_mutability() -> None:
    v = scoring()
    platforms, age = v.question("platforms"), v.question("age")
    assert platforms is not None and platforms.mutable and platforms.cap == 8
    assert age is not None and not age.mutable
    assert v.deduction("platforms", "Instagram") == 3
    assert v.deduction("platforms", "Bumble") is None and v.deduction("nope", "x") is None
    assert v.mapped_tags == frozenset({"instagram"})
    assert v.registry().active == frozenset({"instagram", "linkedin"})
    assert v.registry().retired == frozenset({"myspace"})


def test_a_step_one_document_without_tag_kinds_or_questions_still_parses() -> None:
    parsed = parse_vocabulary(
        Vocabulary(
            release_no=1, map_version=1, scoring_version="s", quiz_version="q", document=VOCABULARY
        )
    )
    assert parsed is not None and dict(parsed.questions) == {}


@pytest.mark.parametrize(
    "questions",
    [
        [{"key": "q", "prompt": "p", "type": "mutable"}],  # no options
        [
            {
                "key": "q",
                "prompt": "p",
                "type": "mutable",
                "options": ["a"],
                "deductions": {"a": "3"},
                "cap": None,
            }
        ],  # a deduction that is not an int
        [
            {
                "key": "q",
                "prompt": "p",
                "type": "mutable",
                "options": ["a"],
                "deductions": {"a": True},
                "cap": None,
            }
        ],  # a bool is not a deduction
    ],
)
def test_an_unreadable_document_parses_to_none(questions: list[dict[str, object]]) -> None:
    row = Vocabulary(
        release_no=1,
        map_version=1,
        scoring_version="s",
        quiz_version="q",
        document=quiz_document(questions=questions),
    )
    assert parse_vocabulary(row) is None


def test_normalise_subject_is_exact_never_fuzzy() -> None:
    assert normalise_subject("X (Twitter)") == normalise_subject("x_twitter") == "x twitter"
    assert normalise_subject("  Bumble!! ") == "bumble"
    assert normalise_subject("Bumble") != normalise_subject("Bumbl")


def test_renames_fold_in_release_order_and_chain() -> None:
    v = scoring(quiz_document(renamed=_renamed([(5, "A", "B"), (6, "B", "C")])))
    assert v.fold_renames("platforms", "A", after_release_no=4) == "C"
    assert v.fold_renames("platforms", "A", after_release_no=5) == "A"  # release 5 already applied
    assert v.fold_renames("platforms", "B", after_release_no=5) == "C"
    assert v.fold_renames("dating", "A", after_release_no=0) == "A"  # another question's log


def test_a_swap_inside_one_release_is_applied_simultaneously() -> None:
    """Review Focus 4: sequential application would take A -> B -> A."""
    v = scoring(quiz_document(renamed=_renamed([(7, "A", "B"), (7, "B", "A")])))
    assert v.fold_renames("platforms", "A", after_release_no=6) == "B"
    assert v.fold_renames("platforms", "B", after_release_no=6) == "A"


def test_subject_is_mapped_only_for_a_mapped_tags_slug_or_label() -> None:
    v = scoring()
    assert v.subject_is_mapped("instagram")
    assert not v.subject_is_mapped("linkedin")  # registered, unmapped
    assert not v.subject_is_mapped("bumble")  # unregistered


@pytest.mark.parametrize(
    ("question_key", "option", "current", "delta", "expected"),
    [
        ("nope", "Instagram", 3, 1, "unknown_question"),
        ("age", "Under 25", 5, 1, "not_mutable"),
        ("platforms", "Bumble", 3, 1, "unknown_option"),
        ("platforms", "Instagram", 2, 1, "current_mismatch"),
        ("platforms", "Instagram", 3, 0, "delta_out_of_bounds"),
        ("platforms", "Instagram", 3, 3, "delta_out_of_bounds"),
        ("platforms", "Instagram", 3, -3, "delta_out_of_bounds"),
        ("platforms", "Snapchat", 7, 2, "result_out_of_bounds"),  # 9 > cap 8
        ("platforms", "Threads", 1, -2, "result_out_of_bounds"),  # below 0
        ("dating", "Yes", 9, 2, "result_out_of_bounds"),  # above 10, no cap
        ("platforms", "Instagram", 3, -2, None),
        ("platforms", "X (Twitter)", 4, 2, None),
        ("dating", "Yes", 9, 1, None),
    ],
)
def test_cell_problem_in_spec_order(
    question_key: str, option: str, current: int, delta: int, expected: str | None
) -> None:
    assert (
        cell_problem(
            scoring(), question_key=question_key, option=option, current=current, delta=delta
        )
        == expected
    )


def test_stale_reasons() -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    platforms = doc["questions"][0]
    platforms["options"] = ["Instagram (Meta)", "LinkedIn", "X (Twitter)", "Snapchat"]
    platforms["deductions"] = {
        "Instagram (Meta)": 3,
        "LinkedIn": 5,
        "X (Twitter)": 4,
        "Snapchat": 7,
    }
    doc["renamed"] = _renamed([(3, "Instagram", "Instagram (Meta)")])
    doc["questions"][1]["type"] = "escrowed"  # dating is no longer mutable
    v = scoring(doc, release_no=3)

    def reason(key: str, option: str, current: int) -> str | None:
        return stale_reason(
            v, question_key=key, option=option, current=current, generated_at_release=2
        )

    assert reason("platforms", "Instagram", 3) == "option_renamed"
    assert reason("platforms", "Threads", 1) == "cell_removed"
    assert reason("gone", "x", 1) == "cell_removed"
    assert reason("dating", "Yes", 9) == "not_mutable"
    assert reason("platforms", "LinkedIn", 2) == "deduction_moved"
    assert reason("platforms", "X (Twitter)", 4) is None


def test_published_unacknowledged_is_exact_equality() -> None:
    v = scoring()  # Instagram is 3
    assert published_unacknowledged(
        v, question_key="platforms", option="Instagram", current=2, delta=1
    )
    assert not published_unacknowledged(
        v, question_key="platforms", option="Instagram", current=3, delta=1
    )
    assert not published_unacknowledged(
        v, question_key="platforms", option="Bumble", current=2, delta=1
    )
