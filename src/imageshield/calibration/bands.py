"""The banding rules. Pure functions — no DB, no I/O, no clock.

Two decisions live here and nothing else may make them:

  band_for_attestation  one provider's raw value -> a band
  roll_up               several providers' bands -> the infringement's band

**Nothing in this module raises.** Every failure mode — no config, a shape
mismatch, an impossible score, an unknown category — returns ``review``.
Banding sits in the write path of a scan, and a crash there would fail a run
over a provider's malformed row. ``review`` means a human looks, which is the
correct outcome for anything we do not understand.

**No arithmetic across providers.** Nothing here combines two providers'
scores — not by summing them, not by taking a central value, not by
comparing them. The only combination is max-of-ordinal-band. Provider A's
0.92 and Provider B's 0.92 are different quantities with different
distributions (CLAUDE.md §7.2), and combining them yields a meaningless
number that looks entirely plausible.

Phrased without the forbidden words on purpose: ``test_boundaries.py`` greps
this directory for them and has no allowlist, which is the point.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from imageshield.calibration.models import (
    BAND_ORDER,
    Band,
    BandDecision,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
    ScoreKind,
)

_REVIEW_NO_CONFIG = BandDecision(
    band="review", reason="no_active_config", calibration_version=None
)


def _review(reason: str, version: str | None) -> BandDecision:
    return BandDecision(band="review", reason=reason, calibration_version=version)


def band_for_attestation(
    entry: PolicyEntry | None,
    score_kind: ScoreKind,
    provider_score: Decimal | None,
    provider_category: str | None,
) -> BandDecision:
    """Rules in order, first match wins. See the spec's §5.1 table.

    ``entry`` is None when the provider has no row in the policy snapshot at
    all — treated identically to having no active config.
    """
    # Rule 1 — no config has told us how to interpret this provider's numbers.
    if entry is None or entry.config is None:
        return _REVIEW_NO_CONFIG
    config = entry.config
    version = config.version

    # Rule 2 — CLAUDE.md §7.3. A provider we have not measured against a
    # labelled set must not be able to tell someone their face is in porn
    # without a human looking first. This blocks `drop` as well: "review band
    # only" is literal, and drop is the more dangerous edge because a real
    # infringement landing there is invisible to the user forever.
    if not entry.calibrated:
        return _review("provider_uncalibrated", version)

    # Rule 3 — the config was written for a different score shape, or the
    # adapter produced neither shape's required value.
    if config.score_kind != score_kind:
        return _review("score_kind_mismatch", version)

    if score_kind == "numeric":
        if provider_score is None:
            return _review("score_kind_mismatch", version)
        # Rule 3b — guard non-finite input before any comparison. NUMERIC
        # columns accept 'NaN'/'Infinity' and provider_score is an
        # unvalidated Decimal; comparing a NaN against band edges is a
        # distinct, diagnosable problem from an in-range-but-impossible
        # value, so it gets its own reason rather than falling into
        # score_out_of_domain.
        if not provider_score.is_finite():
            return _review("score_not_finite", version)
        domain = entry.score_domain
        # Rule 3c — a domain missing EITHER bound (no row in
        # `providers.score_domain`, a half-open range, or one with neither
        # bound set) means we do not know how to interpret the numbers.
        # This is a precondition of the algorithm, not defensive padding:
        # I1's inclusive-max rule for the top band is defined in terms of
        # `domain.max` (`_numeric_band` below), so with no max there is no
        # defined top-of-domain and the banding semantics are genuinely
        # undefined, not merely unvalidated. Requiring both bounds also
        # makes this agree with `validate_numeric_bands`, which already
        # rejects a config against a domain missing either bound (a
        # half-open domain still lets `_in_domain` treat the unbounded side
        # as "everything passes", so an impossible value like -5 would be
        # read as a low score and silently banded `drop` on that side too —
        # exactly the failure this module exists to prevent, reached through
        # a missing/partial config row instead of through code).
        if domain.min is None or domain.max is None:
            return _review("score_domain_unknown", version)
        # Rule 4 — a value the provider cannot legitimately report.
        if not _in_domain(provider_score, domain):
            return _review("score_out_of_domain", version)
        # Rule 5
        band = _numeric_band(provider_score, config.numeric_bands, domain)
        if band is None:
            # Bands do not cover this in-domain value. propose() rejects such
            # a config, so this is a config that predates validation or was
            # inserted by hand.
            return _review("no_band_covers_score", version)
        return BandDecision(
            band=band, reason=f"numeric:{band}", calibration_version=version
        )

    # Rules 6 & 7 — categorical. provider_score is ignored entirely; the
    # provider reported no number and we do not invent one.
    if provider_category is None:
        return _review("score_kind_mismatch", version)
    mapped = config.categorical_bands.get(provider_category)
    if mapped is None:
        return _review("unknown_category", version)
    return BandDecision(
        band=mapped, reason=f"categorical:{mapped}", calibration_version=version
    )


def _in_domain(score: Decimal, domain: ScoreDomain) -> bool:
    if domain.min is not None and score < domain.min:
        return False
    if domain.max is not None and score > domain.max:
        return False
    return True


def _numeric_band(
    score: Decimal, bands: Sequence[NumericBand], domain: ScoreDomain
) -> Band | None:
    """``min`` inclusive, ``max`` exclusive.

    The one exception is the band that owns the top of the *domain* — the
    band whose configured ``max`` reaches or exceeds ``domain.max``. Its
    ``max`` is inclusive there, so the domain ceiling itself lands somewhere
    instead of falling through to ``no_band_covers_score``. A band whose
    ``max`` sits strictly below the domain ceiling does NOT get this
    treatment, even if no other configured band starts above it — a value at
    that boundary belongs to whatever comes next (another band, or nothing,
    which is ``no_band_covers_score`` and correctly forces review). Checking
    against *configured* band starts rather than against ``domain.max``
    directly was the bug: it let a band whose ``max`` was below the domain
    ceiling claim inclusivity it was never entitled to.
    """
    for band in bands:
        if band.min is not None and score < band.min:
            continue
        if band.max is not None:
            tops_domain = domain.max is None or band.max >= domain.max
            if score > band.max:
                continue
            if score == band.max and not tops_domain:
                continue
        return band.band
    return None


def roll_up(bands: Sequence[Band]) -> tuple[Band, str]:
    """Several providers' bands -> the infringement's band, plus the reason.

    - **Disagreement resolves downward.** Any spread at all yields ``review``
      — ``drop`` + ``auto_confirm``, and equally ``review`` +
      ``auto_confirm``. Providers disagreeing is evidence of uncertainty, and
      uncertainty means a human looks.
    - **Agreement does not promote.** Two providers at ``review`` stay
      ``review``. Concurrence between two image-search providers indexing
      overlapping corpora is not two independent observations.

    Evidence moves a band down easily and up reluctantly. That asymmetry is
    deliberate and is the correct bias for this product.

    Total against its input type at runtime, not just at the type level:
    these values are read back out of the database, and a value that is not
    one of the three known bands (a bad row, a future band added to the
    CHECK constraint but not here yet, even a bare string silently iterated
    character-by-character by a caller who forgot to wrap it in a list)
    resolves to ``review`` rather than raising ``KeyError`` — the safe
    direction, same as everything else in this module.
    """
    if not bands:
        # Unreachable — the write path always creates an attestation with the
        # infringement — but a total function has no unreachable branches.
        return "review", "no_attestations"
    for b in bands:
        if b not in BAND_ORDER:
            return "review", f"unknown_band:{b}"
    lowest = min(bands, key=lambda b: BAND_ORDER[b])
    highest = max(bands, key=lambda b: BAND_ORDER[b])
    if lowest != highest:
        return "review", f"disagreement:{lowest}|{highest}->review"
    return highest, f"unanimous:{highest}(n={len(bands)})"


def validate_numeric_bands(
    bands: Sequence[NumericBand], domain: ScoreDomain
) -> list[str]:
    """Problems with a candidate band set, empty when valid. Used by
    ``propose`` — a config that tiles the domain wrongly would silently send
    scores to ``no_band_covers_score`` at runtime."""
    problems: list[str] = []
    # A band whose min equals its max is zero-width and, given
    # `_numeric_band`'s min-inclusive/max-exclusive rule, can never fire —
    # drop(max=0.72), review(0.72, 0.72), auto_confirm(min=0.72) tiles clean
    # by every other check here while `review` is dead code. Checked
    # unconditionally, independent of the domain checks below.
    for b in bands:
        if b.min is not None and b.max is not None and b.min == b.max:
            problems.append(
                f"band {b.band} has zero width (min == max == {b.min}) and "
                "can never fire"
            )
    if domain.min is None or domain.max is None:
        # Coverage (gap/overlap) below is computed by walking a cursor from
        # domain.min to domain.max; with either bound missing there is no
        # floor or ceiling to walk from, and the walk silently no-ops rather
        # than catching a real gap (an unbounded domain also describes a
        # categorical-only ScoreDomain, against which numeric bands are
        # nonsensical regardless of gaps). Report it and stop rather than
        # claim a set of bands is valid when it was never actually checked.
        problems.append(
            f"score_domain [{domain.min}, {domain.max}] has no lower and "
            "upper bound both set; coverage cannot be validated against an "
            "unbounded domain"
        )
        return problems
    for b in bands:
        for edge, value in (("min", b.min), ("max", b.max)):
            if value is None:
                continue
            if not _in_domain(value, domain):
                problems.append(
                    f"band {b.band} {edge}={value} is outside score_domain "
                    f"[{domain.min}, {domain.max}]"
                )
    ordered = sorted(
        bands, key=lambda b: b.min if b.min is not None else Decimal("-1e9")
    )
    cursor = domain.min
    for b in ordered:
        start = b.min if b.min is not None else domain.min
        if cursor is not None and start is not None:
            if start > cursor:
                problems.append(f"gap in coverage between {cursor} and {start}")
            elif start < cursor:
                problems.append(f"overlap at {start}: already covered up to {cursor}")
        cursor = b.max if b.max is not None else domain.max
    if cursor is not None and domain.max is not None and cursor < domain.max:
        problems.append(f"gap in coverage between {cursor} and {domain.max}")
    return problems
