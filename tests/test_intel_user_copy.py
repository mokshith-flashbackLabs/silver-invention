"""The score page's word rules (spec 2026-10-11-intel-score-insights §4.1-4.2). Pure."""

from __future__ import annotations

import pytest

from imageshield.intel.user_copy import WORD_RULES, copy_problems, figures, unquoted_figures


def test_a_plain_risk_line_passes() -> None:
    assert copy_problems("Young adults are targeted most often in reported deepfake cases.") == []


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("You are safe once you do this.", "reassurance"),
        ("This gives you an all-clear.", "reassurance"),
        ("Guaranteed protection for your likeness.", "reassurance"),
        ("Images like yours were found on forums.", "finding"),
        ("A face match was detected.", "finding"),
        ("Hits rise every year.", "finding"),
        ("Your photos are at risk.", "finding"),
        ("You answered that you post weekly.", "answer"),
        ("Your answer raises the risk.", "answer"),
        ("Seventy percent of victims are women.", "spelled_figure"),
        ("Read more at help.example.com today.", "address"),
        ("Visit www.example.org for details.", "address"),
        ("See https://example.org.", "address"),
        ("Write to someone@example.org.", "address"),
        ("Call +1 555 123 4567 now.", "address"),
        ("Reference 12345678901 applies.", "address"),
        ("Ask «masked» for help.", "address"),
    ],
)
def test_each_rule_refuses_its_words(text: str, rule: str) -> None:
    assert rule in copy_problems(text)


def test_the_list_is_the_five_categories_in_order() -> None:
    assert [rule for rule, _ in WORD_RULES] == [
        "reassurance",
        "finding",
        "answer",
        "spelled_figure",
        "address",
    ]


def test_safety_settings_and_matching_words_inside_others_pass() -> None:
    assert copy_problems("Review your safety settings and rematch nothing.") == []


def test_years_are_not_phone_numbers() -> None:
    assert copy_problems("Reports rose between 2023-2024 and 2025.") == []


def test_figures_are_read_with_their_percent_and_without_thousands_separators() -> None:
    assert [(f.core, f.percent) for f in figures("75% of 1,200 cases in 2024.")] == [
        ("75", True),
        ("1200", False),
        ("2024", False),
    ]


def test_a_quoted_figure_passes_and_an_unquoted_one_is_named() -> None:
    quotes = ["Of 1200 reports, 96% involved women aged 19 to 35."]
    assert unquoted_figures("96% of reports involve women aged 19-35.", quotes) == []
    assert unquoted_figures("1,200 reports were made.", quotes) == []
    assert unquoted_figures("75% of reports involve women.", quotes) == ["75%"]


def test_a_percent_needs_a_percent_in_the_quote_but_a_bare_number_does_not() -> None:
    assert unquoted_figures("75% of cases", ["75 per cent of cases"]) == []
    assert unquoted_figures("75% of cases", ["75 cases"]) == ["75%"]
    assert unquoted_figures("75 cases", ["75% of cases"]) == []


def test_a_figure_is_a_token_not_a_substring() -> None:
    assert unquoted_figures("5 platforms", ["Reported in 2025."]) == ["5"]


def test_text_without_digits_always_passes() -> None:
    assert unquoted_figures("Public profiles are scraped more often.", []) == []


# -- final review I5 (2026-10-12): spelled-out figures and singular "your photo" --


@pytest.mark.parametrize(
    "text",
    [
        "Nine in ten victims are women.",
        "One out of five reports involves a minor.",
        "Three quarters of cases start on social media.",
        "Half of all reports name a dating app.",
        "Women are twice as likely to be targeted.",
    ],
)
def test_a_spelled_out_figure_is_refused(text: str) -> None:
    assert "spelled_figure" in copy_problems(text)


@pytest.mark.parametrize(
    "text",
    ["Your photo was spotted on forums.", "Your images are copied.", "Your picture is at risk."],
)
def test_the_reader_s_photo_in_any_form_is_a_finding(text: str) -> None:
    assert "finding" in copy_problems(text)


def test_two_factor_and_behalf_of_still_pass() -> None:
    assert copy_problems("Turn on two-factor sign-in on behalf of your account.") == []
