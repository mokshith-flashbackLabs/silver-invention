"""Calibration metrics. Pure functions, no database.

One rule governs this module: **a figure never leaves it without its sample
size**. `Metric` cannot represent a bare proportion, so a precision of 1.0
over 40 items renders as ``1.000 (40/40, 95% lower bound 0.912)`` and reads
like what it is — a weak signal — rather than like a passing grade.

The Wilson lower bound is displayed, not gated on. The activate floor tests
the point estimate plus a minimum sample size (spec §6.5); gating on the
lower bound instead is a possible future tightening, deliberately not taken
now.

Counting rule that matters more than it looks: **predicted-positive means an
observation exists AND clears the threshold.** An eval item with no
observation is predicted negative. That is how a true_match the provider
never returned becomes a false negative rather than vanishing from the
denominator — and it is why eval_seed_coverage has to exist, since without it
"not returned" and "never asked" are indistinguishable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from imageshield.calibration.models import (
    Band,
    Label,
    LabelKind,
    ScoreDomain,
    TRUE_MATCH_KINDS,
)

# 97.5th percentile of the standard normal — the two-sided 95% interval.
_Z_95 = 1.959963984540054

# "No threshold applies": categorical providers report no number, so an item
# counts as found when the provider returned it at all. A named constant
# rather than a magic -1e9 sprinkled through the call sites.
_NO_THRESHOLD = Decimal("-Infinity")


class Metric(BaseModel):
    """A proportion that cannot be reported without its denominator."""

    model_config = ConfigDict(frozen=True)

    value: float | None
    numerator: int
    denominator: int
    wilson_lower_95: float | None

    def render(self) -> str:
        if self.value is None:
            return f"n/a (0/{self.denominator})"
        lower = (
            f", 95% lower bound {self.wilson_lower_95:.3f}"
            if self.wilson_lower_95 is not None
            else ""
        )
        return f"{self.value:.3f} ({self.numerator}/{self.denominator}{lower})"


def metric(numerator: int, denominator: int) -> Metric:
    if denominator <= 0:
        # None, never 0.0: "unmeasured" and "measured and terrible" are
        # different claims and must not render the same.
        return Metric(value=None, numerator=numerator, denominator=denominator,
                      wilson_lower_95=None)
    return Metric(
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        wilson_lower_95=_wilson_lower_95(numerator, denominator),
    )


def _wilson_lower_95(k: int, n: int) -> float:
    """Wilson score interval, lower edge. Chosen over the normal
    approximation because it stays sane at p = 1.0, which is exactly the case
    a small eval set produces."""
    p = k / n
    denom = 1.0 + _Z_95 * _Z_95 / n
    centre = p + _Z_95 * _Z_95 / (2 * n)
    margin = _Z_95 * math.sqrt(p * (1.0 - p) / n + _Z_95 * _Z_95 / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


class EvalRow(BaseModel):
    """One eval_item LEFT JOINed to this provider's observation of it.

    ``observed=False`` means the provider was asked (eval_seed_coverage says
    so) and did not return this candidate.
    """

    model_config = ConfigDict(frozen=True)

    label: Label
    label_kind: LabelKind
    observed: bool
    provider_score: Decimal | None
    provider_category: str | None


class Confusion(BaseModel):
    model_config = ConfigDict(frozen=True)

    tp: int
    fp: int
    fn: int
    tn: int
    uncertain: int


def precision(c: Confusion) -> Metric:
    return metric(c.tp, c.tp + c.fp)


def recall(c: Confusion) -> Metric:
    return metric(c.tp, c.tp + c.fn)


def npv(c: Confusion) -> Metric:
    return metric(c.tn, c.tn + c.fn)


def _tally(rows: Sequence[EvalRow], predicted: Sequence[bool]) -> Confusion:
    tp = fp = fn = tn = unc = 0
    for row, positive in zip(rows, predicted, strict=True):
        if row.label == "uncertain":
            unc += 1
            continue
        is_positive = row.label == "true_match"
        if positive:
            tp += is_positive
            fp += not is_positive
        else:
            fn += is_positive
            tn += not is_positive
    return Confusion(tp=tp, fp=fp, fn=fn, tn=tn, uncertain=unc)


def confusion_at_threshold(rows: Sequence[EvalRow], threshold: Decimal) -> Confusion:
    """Predicted positive = observed AND score >= threshold."""
    predicted = [
        row.observed and row.provider_score is not None and row.provider_score >= threshold
        for row in rows
    ]
    return _tally(rows, predicted)


def confusion_for_categories(
    rows: Sequence[EvalRow], positive: frozenset[str]
) -> Confusion:
    predicted = [
        row.observed
        and row.provider_category is not None
        and row.provider_category in positive
        for row in rows
    ]
    return _tally(rows, predicted)


def effective_sample_size(rows: Sequence[EvalRow]) -> int:
    """Items that actually enter the arithmetic. The activate floor counts
    these, not raw rows — 200 rows of which 150 are uncertain is 50 items of
    evidence."""
    return sum(1 for row in rows if row.label != "uncertain")


class SetComposition(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    observed: int
    uncertain: int
    by_label_kind: Mapping[LabelKind, int]
    lookalike_count: int

    def render(self) -> str:
        kinds = "  ".join(f"{k} {v}" for k, v in sorted(self.by_label_kind.items()))
        return (
            f"items {self.total}   observations {self.observed}   "
            f"uncertain {self.uncertain} (excluded)\n  {kinds}"
        )


def composition(rows: Sequence[EvalRow]) -> SetComposition:
    counts: dict[LabelKind, int] = {}
    for row in rows:
        counts[row.label_kind] = counts.get(row.label_kind, 0) + 1
    return SetComposition(
        total=len(rows),
        observed=sum(1 for r in rows if r.observed),
        uncertain=sum(1 for r in rows if r.label == "uncertain"),
        by_label_kind=counts,
        lookalike_count=counts.get("lookalike", 0),
    )


def recall_by_label_kind(
    rows: Sequence[EvalRow], threshold: Decimal
) -> dict[LabelKind, Metric]:
    """Recall over positives only, split by kind.

    A nudify edit preserves background, body, and composition, so image search
    plausibly finds it. A novel generation shares no pixels with anything we
    hold and recall there will be near zero. One averaged figure hides that;
    reporting the split puts the product's real coverage gap in every
    calibration report as a number.
    """
    out: dict[LabelKind, Metric] = {}
    for kind in sorted(TRUE_MATCH_KINDS):
        subset = [r for r in rows if r.label_kind == kind and r.label == "true_match"]
        if not subset:
            continue
        found = sum(
            1
            for r in subset
            if r.observed
            and (
                r.provider_score is None or r.provider_score >= threshold
            )
        )
        out[kind] = metric(found, len(subset))
    return out


class ThresholdPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    threshold: Decimal
    precision_at_or_above: Metric
    recall_at_or_above: Metric
    npv_below: Metric


class NumericSweep(BaseModel):
    model_config = ConfigDict(frozen=True)

    composition: SetComposition
    points: tuple[ThresholdPoint, ...]
    recall_by_kind: Mapping[LabelKind, Metric]
    recommended_auto_confirm_min: Decimal | None
    recommended_drop_max: Decimal | None
    warnings: tuple[str, ...]


def sweep_numeric(
    rows: Sequence[EvalRow],
    domain: ScoreDomain,
    target: Decimal = Decimal("0.99"),
) -> NumericSweep:
    """Candidate boundaries are the observed scores plus the domain edges."""
    candidates = sorted(
        {r.provider_score for r in rows if r.provider_score is not None}
        | {v for v in (domain.min, domain.max) if v is not None}
    )
    points: list[ThresholdPoint] = []
    for t in candidates:
        c = confusion_at_threshold(rows, t)
        # NPV below t is the mirror image: everything NOT predicted positive.
        points.append(
            ThresholdPoint(
                threshold=t,
                precision_at_or_above=precision(c),
                recall_at_or_above=recall(c),
                npv_below=npv(c),
            )
        )

    warnings: list[str] = []
    comp = composition(rows)
    if comp.lookalike_count == 0:
        warnings.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure. Random "
            "negatives are easy for any provider to reject and will make a "
            "bad threshold look excellent."
        )

    target_f = float(target)
    # Lowest boundary clearing the precision floor: subject to that floor,
    # more recall is strictly better.
    auto_min = next(
        (
            p.threshold
            for p in points
            if p.precision_at_or_above.value is not None
            and p.precision_at_or_above.value >= target_f
        ),
        None,
    )
    # Highest boundary clearing the NPV floor: subject to it, dropping more
    # saves more review capacity.
    drop_max = next(
        (
            p.threshold
            for p in reversed(points)
            if p.npv_below.value is not None and p.npv_below.value >= target_f
        ),
        None,
    )
    if auto_min is None:
        warnings.append(
            f"no threshold reaches precision >= {target} — auto_confirm is not "
            "supportable by this set. The provider stays uncalibrated and "
            "everything stays review."
        )
    if drop_max is None:
        warnings.append(
            f"no threshold reaches NPV >= {target} — drop is not supportable "
            "by this set."
        )
    if auto_min is not None and drop_max is not None and auto_min < drop_max:
        warnings.append(
            f"recommended bands overlap (drop < {drop_max}, auto_confirm >= "
            f"{auto_min}); no recommendation issued"
        )
        auto_min = None
        drop_max = None

    return NumericSweep(
        composition=comp,
        points=tuple(points),
        # Recall is reported at the recommended auto_confirm boundary when
        # there is one, and at "returned at all" when there is not — a recall
        # figure at a threshold nobody would ship is not informative.
        recall_by_kind=recall_by_label_kind(
            rows, auto_min if auto_min is not None else _NO_THRESHOLD
        ),
        recommended_auto_confirm_min=auto_min,
        recommended_drop_max=drop_max,
        warnings=tuple(warnings),
    )


class CategoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    count: int
    precision: Metric


class CategoricalSweep(BaseModel):
    model_config = ConfigDict(frozen=True)

    composition: SetComposition
    points: tuple[CategoryPoint, ...]
    recall_by_kind: Mapping[LabelKind, Metric]
    recommended: Mapping[str, Band]
    warnings: tuple[str, ...]


def sweep_categorical(
    rows: Sequence[EvalRow],
    categories: Sequence[str],
    target: Decimal = Decimal("0.99"),
) -> CategoricalSweep:
    """Greedy and deterministic, documented as such: auto_confirm any category
    whose own precision clears the floor; then drop the remaining categories,
    largest-NPV-first, while the drop set keeps NPV at the floor."""
    target_f = float(target)
    points: list[CategoryPoint] = []
    for category in categories:
        c = confusion_for_categories(rows, frozenset({category}))
        points.append(
            CategoryPoint(
                category=category,
                count=sum(1 for r in rows if r.provider_category == category),
                precision=precision(c),
            )
        )

    recommended: dict[str, Band] = {c: "review" for c in categories}
    for p in points:
        if p.precision.value is not None and p.precision.value >= target_f:
            recommended[p.category] = "auto_confirm"

    remaining = [p for p in points if recommended[p.category] == "review"]
    drop_set: set[str] = set()
    for p in sorted(remaining, key=lambda x: x.precision.value or 0.0):
        if p.count == 0:
            # A category with no items at all contributes nothing to the
            # confusion counts whether or not it joins the drop set — the
            # totals-unchanged guard below would pass trivially and drop it
            # for free. Zero items is neither evidence of safety nor of
            # danger; it stays `review`.
            continue
        candidate = drop_set | {p.category}
        # NPV of "predicted negative" when the positive set is everything not
        # dropped: an item is predicted negative exactly when it landed in a
        # dropped category.
        c = confusion_for_categories(rows, frozenset(candidate))
        # Flip: precision over the drop set measures how many true matches we
        # would be discarding, so NPV of the drop decision is 1 - that.
        wrong = c.tp                       # true matches inside the drop set
        total = c.tp + c.fp
        if total == 0:
            continue
        if ((total - wrong) / total) >= target_f:
            drop_set = candidate
    for category in drop_set:
        recommended[category] = "drop"

    comp = composition(rows)
    warnings: list[str] = []
    if comp.lookalike_count == 0:
        warnings.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure."
        )
    return CategoricalSweep(
        composition=comp,
        points=tuple(points),
        # A categorical row has no score, so "found" means the provider
        # returned it at all — _NO_THRESHOLD makes that explicit rather than
        # relying on a magic number.
        recall_by_kind=recall_by_label_kind(rows, _NO_THRESHOLD),
        recommended=recommended,
        warnings=tuple(warnings),
    )
