"""Sweep rendering. Pure string formatting over Task 3's metrics.

The report is the artifact a human reads before deciding whether a provider
may alarm people unreviewed, so what it refuses to omit is load-bearing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from imageshield.calibration.metrics import EvalRow, sweep_categorical, sweep_numeric
from imageshield.calibration.models import ScoreDomain
from imageshield.calibration.report import (
    render_categorical_sweep,
    render_numeric_sweep,
)

HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))


def num(label: str, kind: str, score: str | None) -> EvalRow:
    return EvalRow(
        label=label, label_kind=kind, observed=score is not None,
        provider_score=Decimal(score) if score is not None else None,
        provider_category=None,
    )


def test_report_opens_with_composition_before_any_metric() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.60"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert text.index("items 2") < text.index("precision")


@pytest.mark.parametrize(
    "false_match_score",
    [
        # Separable: produces a recommendation. Exercises the table rows and
        # the header, but never metrics.py's "no threshold reaches ..."
        # warning — that line renders only when NO boundary can be found at
        # all, which this fixture alone never triggers.
        "0.60",
        # Identical to the true_match score: no threshold can ever separate
        # them, so this is the fixture that actually renders metrics.py's
        # precision-target warning — the line this invariant exists to
        # guard against a regression in.
        "0.95",
    ],
)
def test_no_figure_appears_without_its_sample_size(false_match_score: str) -> None:
    """Every proportion in the body carries (n/d). A bare 0.975 in this
    output would be read as a result rather than as two observations.

    A fixed *target* (e.g. "the required precision target (0.99)") is exempt
    — 0.99 is a threshold the sweep was asked to clear, not a measurement
    with a sample size of its own, so it legitimately carries no numerator/
    denominator and is not what this rule is guarding against.
    """
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", false_match_score),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    for line in text.splitlines():
        if "precision" in line and "n/a" not in line and "target" not in line:
            assert "/" in line, line


def test_zero_lookalikes_is_shouted_not_footnoted() -> None:
    rows = [
        num("true_match", "same_person", "0.99"),
        num("false_match", "unrelated", "0.60"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "ZERO lookalike" in text
    assert "WARNING" in text


def test_uncovered_seeds_are_listed() -> None:
    rows = [num("true_match", "same_person", "0.95")]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1",
        uncovered=("s3://seed-b", "s3://seed-c"),
    )
    assert "2 seed(s) never successfully observed" in text
    assert "s3://seed-b" in text


def test_report_says_so_when_no_recommendation_is_possible() -> None:
    """The required outcome when the set cannot demonstrate 0.99 — reported
    plainly, not by quietly emitting a looser boundary."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.95"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "no auto_confirm boundary" in text
    assert "stays uncalibrated" in text


def test_recall_by_label_kind_appears_in_the_report() -> None:
    rows = [
        num("true_match", "same_person", "0.99"),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.55"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "novel_generation" in text


def test_categorical_report_shows_the_recommended_mapping() -> None:
    rows = [
        EvalRow(label="true_match", label_kind="same_person", observed=True,
                provider_score=None, provider_category="full_match"),
        EvalRow(label="false_match", label_kind="lookalike", observed=True,
                provider_score=None, provider_category="partial_match"),
    ]
    text = render_categorical_sweep(
        sweep_categorical(rows, ("full_match", "partial_match", "page_match")),
        "google", "v1", uncovered=(),
    )
    assert "full_match" in text
    assert "page_match" in text
