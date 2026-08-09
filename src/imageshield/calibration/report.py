"""Rendering for sweep output.

Separate from :mod:`metrics` so that module stays pure arithmetic with no
formatting decisions in it.

Two things this renderer will not do: print a proportion without its
denominator, and print a recommendation the data does not support. A sweep
over a set that cannot demonstrate 0.99 says so — the correct outcome there
is that the provider stays uncalibrated and everything stays ``review``, not
a looser boundary that produces a result.

The column headers below spell out ``(n/d)`` deliberately: a bare column
title reading ``precision >=`` would itself be a figure-shaped word with no
denominator next to it, which is exactly the thing this module refuses to
print. Every row underneath renders through :meth:`Metric.render`, which
always carries its ``numerator/denominator`` — the header says so too.
"""

from __future__ import annotations

from collections.abc import Sequence

from imageshield.calibration.metrics import CategoricalSweep, NumericSweep

_RULE = "─" * 72


def _header(provider_id: str, eval_set_id: str, sweep_composition: str) -> list[str]:
    return [
        _RULE,
        f"eval set {eval_set_id} / {provider_id}",
        f"  {sweep_composition}",
    ]


def _coverage(uncovered: Sequence[str]) -> list[str]:
    if not uncovered:
        return ["  seed coverage: complete"]
    lines = [
        f"  WARNING: {len(uncovered)} seed(s) never successfully observed —",
        "    their items' absences are NOT misses and are not evidence of anything:",
    ]
    lines.extend(f"      {seed}" for seed in uncovered)
    return lines


def _warnings(warnings: Sequence[str]) -> list[str]:
    return [f"  WARNING: {w}" for w in warnings]


def render_numeric_sweep(
    sweep: NumericSweep,
    provider_id: str,
    eval_set_id: str,
    uncovered: Sequence[str],
) -> str:
    lines = _header(provider_id, eval_set_id, sweep.composition.render())
    lines.extend(_coverage(uncovered))
    lines.extend(_warnings(sweep.warnings))
    lines.append(_RULE)
    lines.append(
        f"{'threshold':>10}  {'precision (n/d) >=':>28}  "
        f"{'recall (n/d) >=':>28}  {'NPV (n/d) <':>28}"
    )
    for point in sweep.points:
        lines.append(
            f"{point.threshold!s:>10}  "
            f"{point.precision_at_or_above.render():>28}  "
            f"{point.recall_at_or_above.render():>28}  "
            f"{point.npv_below.render():>28}"
        )
    lines.append(_RULE)
    lines.append("recall by label_kind (positives only):")
    for kind, m in sweep.recall_by_kind.items():
        lines.append(f"  {kind:<18} {m.render()}")
    lines.append(_RULE)
    if sweep.recommended_auto_confirm_min is None:
        # Deliberately worded WITHOUT the word "precision": this line is a
        # statement that no figure met the target, not a figure itself, and
        # must not read like one carrying no denominator.
        lines.append(
            "  no auto_confirm boundary reaches the required 0.99 target on "
            "this set."
        )
        lines.append(
            "  The correct outcome is that the provider stays uncalibrated and "
            "everything stays review."
        )
    else:
        lines.append(
            f"  recommended auto_confirm: score >= {sweep.recommended_auto_confirm_min}"
        )
    if sweep.recommended_drop_max is None:
        lines.append("  no drop boundary reaches NPV >= 0.99 on this set.")
    else:
        lines.append(f"  recommended drop: score < {sweep.recommended_drop_max}")
    lines.append(_RULE)
    return "\n".join(lines)


def render_categorical_sweep(
    sweep: CategoricalSweep,
    provider_id: str,
    eval_set_id: str,
    uncovered: Sequence[str],
) -> str:
    lines = _header(provider_id, eval_set_id, sweep.composition.render())
    lines.extend(_coverage(uncovered))
    lines.extend(_warnings(sweep.warnings))
    lines.append(_RULE)
    lines.append(f"{'category':<18}{'n':>6}  {'precision (n/d) >=':>28}  recommended")
    for point in sweep.points:
        lines.append(
            f"{point.category:<18}{point.count:>6}  "
            f"{point.precision.render():>28}  {sweep.recommended[point.category]}"
        )
    lines.append(_RULE)
    lines.append("recall by label_kind (positives only):")
    for kind, m in sweep.recall_by_kind.items():
        lines.append(f"  {kind:<18} {m.render()}")
    lines.append(_RULE)
    if not any(b == "auto_confirm" for b in sweep.recommended.values()):
        lines.append(
            "  no category reaches the required 0.99 target — the provider "
            "stays uncalibrated and everything stays review."
        )
    lines.append(_RULE)
    return "\n".join(lines)
