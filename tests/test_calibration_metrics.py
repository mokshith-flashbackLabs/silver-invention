"""Calibration metrics. Pure, no database.

The load-bearing property: a figure without its sample size is not reportable
here. A precision of 1.0 over 40 items is a weak signal and must never appear
as a bare number.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from imageshield.calibration.metrics import (
    Confusion,
    EvalRow,
    composition,
    confusion_at_threshold,
    effective_sample_size,
    metric,
    npv,
    precision,
    recall,
    recall_by_label_kind,
    sweep_categorical,
    sweep_numeric,
)
from imageshield.calibration.models import ScoreDomain

HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))
GOOGLE_CATEGORIES = ("full_match", "partial_match", "page_match")


def num(label: str, kind: str, score: str | None) -> EvalRow:
    return EvalRow(
        label=label,
        label_kind=kind,
        observed=score is not None,
        provider_score=Decimal(score) if score is not None else None,
        provider_category=None,
    )


def cat(label: str, kind: str, category: str | None) -> EvalRow:
    return EvalRow(
        label=label,
        label_kind=kind,
        observed=category is not None,
        provider_score=None,
        provider_category=category,
    )


# ── Metric: never a bare number ──────────────────────────────────────────

def test_metric_carries_numerator_and_denominator() -> None:
    m = metric(39, 40)
    assert m.value == pytest.approx(0.975)
    assert (m.numerator, m.denominator) == (39, 40)


def test_zero_denominator_is_none_not_zero() -> None:
    """0.0 and "no data" are different claims. A band with no items must not
    report precision 0.0 — that reads as measured-and-terrible rather than
    unmeasured."""
    m = metric(0, 0)
    assert m.value is None
    assert m.wilson_lower_95 is None


def test_wilson_lower_bound_makes_a_small_perfect_set_look_small() -> None:
    """40-for-40 is not evidence of 0.99. The point estimate is 1.0; the
    honest reading is the interval."""
    m = metric(40, 40)
    assert m.value == 1.0
    assert m.wilson_lower_95 is not None
    assert 0.89 < m.wilson_lower_95 < 0.92


def test_wilson_lower_bound_tightens_as_n_grows() -> None:
    small = metric(40, 40).wilson_lower_95
    large = metric(400, 400).wilson_lower_95
    assert small is not None and large is not None
    assert large > small


# ── Confusion counting: a missing observation is a miss, not an absence ──

def test_unreturned_true_match_counts_as_a_false_negative() -> None:
    """The whole reason eval_seed_coverage exists. A true_match the provider
    never returned must land in FN — if it silently left the denominator,
    recall would be computed only over what the provider already found, which
    is guaranteed to look excellent."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("true_match", "novel_generation", None),  # provider missed it
    ]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tp, c.fn) == (1, 1)
    assert recall(c).denominator == 2


def test_unreturned_false_match_counts_as_a_true_negative() -> None:
    rows = [num("false_match", "lookalike", None)]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tn, c.fp) == (1, 0)


def test_uncertain_is_excluded_from_every_cell_and_counted_separately() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("uncertain", "derived_edit", "0.95"),
        num("uncertain", "lookalike", None),
    ]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 0, 0, 0)
    assert c.uncertain == 2


def test_threshold_is_inclusive_at_or_above() -> None:
    rows = [num("true_match", "same_person", "0.90")]
    assert confusion_at_threshold(rows, Decimal("0.90")).tp == 1
    assert confusion_at_threshold(rows, Decimal("0.9001")).fn == 1


def test_precision_recall_npv_arithmetic() -> None:
    c = Confusion(tp=8, fp=2, fn=4, tn=6, uncertain=0)
    assert precision(c).value == pytest.approx(0.8)     # 8/10
    assert recall(c).value == pytest.approx(8 / 12)
    assert npv(c).value == pytest.approx(6 / 10)


def test_effective_sample_size_excludes_uncertain() -> None:
    """The activate floor counts items that actually enter the arithmetic. A
    set of 200 rows where 150 are uncertain is 50 items of evidence."""
    rows = [num("true_match", "same_person", "0.9")] * 3 + [
        num("uncertain", "lookalike", None)
    ] * 2
    assert effective_sample_size(rows) == 3


# ── Composition, reported before any metric ──────────────────────────────

def test_composition_counts_every_label_kind() -> None:
    rows = [
        num("true_match", "same_person", "0.9"),
        num("true_match", "derived_edit", "0.8"),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.7"),
        num("false_match", "unrelated", None),
        num("uncertain", "lookalike", "0.6"),
    ]
    comp = composition(rows)
    assert comp.total == 6
    assert comp.observed == 4
    assert comp.uncertain == 1
    assert comp.by_label_kind["lookalike"] == 2
    assert comp.lookalike_count == 2


def test_zero_lookalikes_produces_a_warning() -> None:
    """Random negatives are easy for any provider to reject and will make a
    bad threshold look excellent. A set without hard negatives cannot produce
    a meaningful precision figure and has to say so."""
    rows = [
        num("true_match", "same_person", "0.99"),
        num("false_match", "unrelated", None),
    ]
    comp = composition(rows)
    assert comp.lookalike_count == 0
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert any("lookalike" in w for w in sweep.warnings)


# ── Recall by label_kind: the coverage gap as a number ───────────────────

def test_recall_is_broken_out_by_label_kind() -> None:
    """A nudify edit preserves background, body, and composition, so image
    search plausibly finds it. A novel generation shares no pixels with
    anything we hold, and recall there will be near zero. Averaging those two
    into one figure hides the product's real limitation."""
    rows = [
        num("true_match", "same_person", "0.99"),
        num("true_match", "same_person", "0.98"),
        num("true_match", "derived_edit", "0.95"),
        num("true_match", "derived_edit", None),
        num("true_match", "novel_generation", None),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.60"),
    ]
    by_kind = recall_by_label_kind(rows, Decimal("0.90"))
    assert by_kind["same_person"].value == pytest.approx(1.0)
    assert by_kind["same_person"].denominator == 2
    assert by_kind["derived_edit"].value == pytest.approx(0.5)
    assert by_kind["novel_generation"].value == pytest.approx(0.0)
    assert by_kind["novel_generation"].denominator == 2
    assert "lookalike" not in by_kind   # recall is over positives only


# ── Numeric sweep and its recommendation ─────────────────────────────────

def test_sweep_reports_a_point_per_observed_score() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.70"),
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    thresholds = {p.threshold for p in sweep.points}
    assert Decimal("0.95") in thresholds
    assert Decimal("0.70") in thresholds


def test_sweep_recommends_the_lowest_threshold_meeting_the_precision_floor() -> None:
    """Lowest, not highest: subject to precision >= 0.99, more recall is
    strictly better, so take the loosest boundary that still clears the bar.

    Needs genuine middle-region ambiguity -- scores at 0.90 that go both
    ways -- so a real review band survives between the two recommendations.
    Verified by hand:
      t=0.96: tp=200,fp=0   -> precision 1.0   (only true_match>=0.96 passes)
      t=0.90: tp=400,fp=200 -> precision 0.667 (both true_match groups pass,
              plus the lookalikes AT 0.90 -- inclusive threshold)
      so auto_min (lowest t with precision>=0.99) is 0.96, not 0.90.
      npv_below(0.90): predicted-negative = lookalike@0.60 only (200) ->
              fn=0, tn=200 -> npv 1.0
      npv_below(0.96): predicted-negative = everything but true_match@0.96
              (600) -> fn=200 (true_match@0.90), tn=400 -> npv 0.667
      so drop_max (highest t with npv>=0.99) is 0.90, not 0.96.
    """
    rows = (
        [num("true_match", "same_person", "0.96") for _ in range(200)]
        + [num("true_match", "same_person", "0.90") for _ in range(200)]
        + [num("false_match", "lookalike", "0.90") for _ in range(200)]
        + [num("false_match", "lookalike", "0.60") for _ in range(200)]
    )
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert sweep.recommended_auto_confirm_min == Decimal("0.96")
    assert sweep.recommended_drop_max == Decimal("0.90")


def test_sweep_refuses_when_boundaries_touch_with_no_review_gap() -> None:
    """Cleanly separated data drives both boundaries to the same value (both
    0.94 here): no score in the eval set falls strictly between drop and
    auto_confirm. The honest read is that this set never measured a review
    band, not that scores can skip straight from drop to auto_confirm --
    real traffic will produce borderline scores this set didn't sample."""
    rows = (
        [num("true_match", "same_person", "0.96") for _ in range(200)]
        + [num("true_match", "same_person", "0.94") for _ in range(200)]
        + [num("false_match", "lookalike", "0.80") for _ in range(200)]
    )
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert sweep.recommended_auto_confirm_min is None
    assert sweep.recommended_drop_max is None
    assert any("review band" in w for w in sweep.warnings)


def test_sweep_refuses_to_recommend_when_the_floor_is_unreachable() -> None:
    """The correct outcome when the data cannot support a band is to say so,
    not to loosen the target until a number appears."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.95"),   # identical score, opposite label
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert sweep.recommended_auto_confirm_min is None
    assert any("precision" in w for w in sweep.warnings)


def test_sweep_refuses_when_recommended_bands_would_overlap() -> None:
    rows = [
        num("true_match", "same_person", "0.60"),
        num("false_match", "lookalike", "0.99"),
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert not (
        sweep.recommended_auto_confirm_min is not None
        and sweep.recommended_drop_max is not None
        and sweep.recommended_auto_confirm_min <= sweep.recommended_drop_max
    )


def test_every_sweep_point_carries_sample_size() -> None:
    """Not just non-negative (vacuous on an int field) -- the denominator at
    each threshold must be the exact count of rows that landed in that
    cell, worked out by hand from the fixture:
      candidates = [0.5, 0.7, 0.95, 1.0] (domain edges + the two scores)
      t=0.5, 0.7: both rows predicted positive -> precision denom 2 (tp+fp),
                  recall denom 1 (tp+fn), npv denom 0 (tn+fn)
      t=0.95:     only the 0.95 true_match clears -> precision denom 1,
                  recall denom 1, npv denom 1
      t=1.0:      neither clears -> precision denom 0, recall denom 1,
                  npv denom 2
    """
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.70"),
    ]
    expected = {
        Decimal("0.5"): (2, 1, 0),
        Decimal("0.7"): (2, 1, 0),
        Decimal("0.95"): (1, 1, 1),
        Decimal("1.0"): (0, 1, 2),
    }
    by_threshold = {p.threshold: p for p in sweep_numeric(rows, HIVE_DOMAIN).points}
    assert set(by_threshold) == set(expected)
    for threshold, (prec_d, rec_d, npv_d) in expected.items():
        p = by_threshold[threshold]
        assert p.precision_at_or_above.denominator == prec_d
        assert p.recall_at_or_above.denominator == rec_d
        assert p.npv_below.denominator == npv_d


# ── Categorical sweep ────────────────────────────────────────────────────

def test_categorical_sweep_reports_precision_per_category() -> None:
    rows = [
        cat("true_match", "same_person", "full_match"),
        cat("true_match", "derived_edit", "partial_match"),
        cat("false_match", "lookalike", "partial_match"),
        cat("false_match", "unrelated", "page_match"),
    ]
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    by_cat = {p.category: p for p in sweep.points}
    assert by_cat["full_match"].precision.value == pytest.approx(1.0)
    assert by_cat["full_match"].precision.denominator == 1
    assert by_cat["partial_match"].precision.value == pytest.approx(0.5)


def test_categorical_sweep_never_reads_provider_score() -> None:
    """A property of the code under test, not of the fixture: a categorical
    row that happens to carry a stray, misleading provider_score must
    produce the exact same result as the same row with no score at all --
    sweep_categorical's precision has to come from provider_category alone."""
    row_with_score = EvalRow(
        label="true_match",
        label_kind="same_person",
        observed=True,
        provider_score=Decimal("0.01"),  # would fail any real threshold
        provider_category="full_match",
    )
    row_without_score = EvalRow(
        label="true_match",
        label_kind="same_person",
        observed=True,
        provider_score=None,
        provider_category="full_match",
    )
    with_score = {
        p.category: p for p in sweep_categorical([row_with_score], GOOGLE_CATEGORIES).points
    }
    without_score = {
        p.category: p
        for p in sweep_categorical([row_without_score], GOOGLE_CATEGORIES).points
    }
    assert with_score["full_match"].precision.value == pytest.approx(1.0)
    assert (
        with_score["full_match"].precision.value
        == without_score["full_match"].precision.value
    )


def test_categorical_recommendation_only_auto_confirms_at_the_floor() -> None:
    rows = [cat("true_match", "same_person", "full_match") for _ in range(200)] + [
        cat("false_match", "lookalike", "partial_match") for _ in range(200)
    ]
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    assert sweep.recommended["full_match"] == "auto_confirm"
    assert sweep.recommended["partial_match"] == "drop"
    # A category with no items at all cannot be promoted or dropped.
    assert sweep.recommended["page_match"] == "review"


def test_all_uncertain_category_is_never_dropped_for_free() -> None:
    """A category whose every row is label='uncertain' contributes nothing
    to any confusion cell -- precision.denominator is 0, real zero evidence.
    Once a real category (partial_match) is already committed to the drop
    set, adding page_match on top of it leaves total/wrong unchanged, so a
    guard that only checks raw row count would let it ride along into
    drop for free. It must stay review."""
    rows = (
        [cat("true_match", "same_person", "full_match") for _ in range(200)]
        + [cat("false_match", "lookalike", "partial_match") for _ in range(200)]
        + [cat("uncertain", "lookalike", "page_match") for _ in range(50)]
    )
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    by_cat = {p.category: p for p in sweep.points}
    assert by_cat["page_match"].precision.denominator == 0
    assert sweep.recommended["page_match"] == "review"


def test_all_unobserved_category_is_never_dropped_for_free() -> None:
    """Same failure mode as the uncertain case, via a different route: a row
    with observed=False can never be predicted positive regardless of its
    category, so it can never contribute to tp/fp for that category either.
    50 such rows tagged page_match must still leave page_match at zero
    evidence, not drop it."""
    rows = (
        [cat("true_match", "same_person", "full_match") for _ in range(200)]
        + [cat("false_match", "lookalike", "partial_match") for _ in range(200)]
        + [
            EvalRow(
                label="false_match",
                label_kind="unrelated",
                observed=False,
                provider_score=None,
                provider_category="page_match",
            )
            for _ in range(50)
        ]
    )
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    by_cat = {p.category: p for p in sweep.points}
    assert by_cat["page_match"].precision.denominator == 0
    assert sweep.recommended["page_match"] == "review"


# ── Numeric and categorical recall must agree on a malformed row ────────

def test_null_score_row_is_treated_consistently_by_confusion_and_recall() -> None:
    """observed=True with provider_score=None is a malformed numeric row --
    the provider answered but supplied no score. confusion_at_threshold
    treats it as a false negative (no score to clear the threshold with).
    recall_by_label_kind must agree: it is not "found" just because the row
    was returned, unlike the categorical case where absence of a score is
    normal, not malformed."""
    rows = [
        EvalRow(
            label="true_match",
            label_kind="same_person",
            observed=True,
            provider_score=None,
            provider_category=None,
        ),
    ]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert c.fn == 1
    assert c.tp == 0
    by_kind = recall_by_label_kind(rows, Decimal("0.90"))
    assert by_kind["same_person"].value == pytest.approx(0.0)
    assert by_kind["same_person"].numerator == 0
    assert by_kind["same_person"].denominator == 1
