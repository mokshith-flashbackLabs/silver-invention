"""Per-attestation banding. Pure functions, no database.

Permanent tests — never delete. Each one corresponds to a way this product
can hurt someone.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from imageshield.calibration.bands import band_for_attestation, validate_numeric_bands
from imageshield.calibration.models import (
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
)
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

# Hive Web Search: 0.5 is the FLOOR of the reported range, not a midpoint.
HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))
GOOGLE_DOMAIN = ScoreDomain(
    categories=("full_match", "partial_match", "page_match")
)

HIVE_BANDS = (
    NumericBand(band="drop", max=Decimal("0.72")),
    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
    NumericBand(band="auto_confirm", min=Decimal("0.94")),
)


def hive_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=HIVE,
        version="hive-cal-v1",
        score_kind="numeric",
        numeric_bands=HIVE_BANDS,
    )


def google_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=GOOGLE,
        version="google-cal-v1",
        score_kind="categorical",
        categorical_bands={
            "full_match": "auto_confirm",
            "partial_match": "review",
            "page_match": "review",
        },
    )


def hive_entry(*, calibrated: bool = True, config: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=HIVE,
        calibrated=calibrated,
        score_domain=HIVE_DOMAIN,
        config=hive_config() if config else None,
    )


def google_entry(*, calibrated: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=GOOGLE,
        calibrated=calibrated,
        score_domain=GOOGLE_DOMAIN,
        config=google_config(),
    )


def band_hive(score: str, **kwargs: bool) -> tuple[str, str]:
    d = band_for_attestation(hive_entry(**kwargs), "numeric", Decimal(score), None)
    return d.band, d.reason


# ── Rule 1 & 2: the gates that force review ──────────────────────────────

def test_no_policy_entry_is_review() -> None:
    d = band_for_attestation(None, "numeric", Decimal("0.99"), None)
    assert d.band == "review"
    assert d.reason == "no_active_config"
    assert d.calibration_version is None


def test_no_active_config_is_review() -> None:
    assert band_hive("0.99", config=False) == ("review", "no_active_config")


@pytest.mark.parametrize("score", ["0.50", "0.71", "0.80", "0.94", "1.00"])
def test_uncalibrated_provider_produces_review_for_every_input(score: str) -> None:
    """CLAUDE.md §7.3. The config says auto_confirm at 0.94+; calibrated=false
    overrides it. This blocks `drop` too — "review band only" is literal, and
    a real infringement landing in drop is invisible to the user forever."""
    band, reason = band_hive(score, calibrated=False)
    assert band == "review"
    assert reason == "provider_uncalibrated"


def test_uncalibrated_categorical_provider_is_also_review() -> None:
    d = band_for_attestation(
        google_entry(calibrated=False), "categorical", None, "full_match"
    )
    assert d.band == "review"
    assert d.reason == "provider_uncalibrated"


# ── Rule 3: shape mismatch never crashes ─────────────────────────────────

def test_score_kind_mismatch_is_review_not_an_exception() -> None:
    d = band_for_attestation(hive_entry(), "categorical", None, "full_match")
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


def test_numeric_kind_with_null_score_is_review() -> None:
    d = band_for_attestation(hive_entry(), "numeric", None, None)
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


# ── Rule 4: the score_domain fixture where it changes the outcome ────────

def test_below_hive_floor_is_review_not_drop() -> None:
    """THE fixture the step-7 done-when asks for.

    0.4 is impossible for Hive — its floor is 0.5. Read against an assumed
    0–1 scale it is merely a low score and bands to `drop`, silently
    discarded and never seen by anyone. Read against score_domain it is a
    malformed response (or a key on the wrong Hive project, which returns
    plausible-looking wrong results rather than an error) and a human looks.
    """
    assert band_hive("0.40") == ("review", "score_out_of_domain")


def test_above_hive_ceiling_is_review_not_auto_confirm() -> None:
    assert band_hive("1.30") == ("review", "score_out_of_domain")


def test_in_domain_low_score_still_drops() -> None:
    """0.60 IS in Hive's domain, so domain-awareness does not suppress a
    genuine low score — it only rejects impossible ones."""
    assert band_hive("0.60") == ("drop", "numeric:drop")


# ── Rule 5: native units, exact boundaries ───────────────────────────────

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.50", "drop"),          # exactly the domain floor
        ("0.7199", "drop"),
        ("0.72", "review"),        # min inclusive
        ("0.9399", "review"),
        ("0.94", "auto_confirm"),  # min inclusive — the boundary that matters
        ("1.00", "auto_confirm"),  # top band's max is inclusive
    ],
)
def test_numeric_boundaries_are_min_inclusive_max_exclusive(
    score: str, expected: str
) -> None:
    assert band_hive(score)[0] == expected


def test_scores_are_never_rescaled() -> None:
    """A 0.72 boundary means 0.72 on Hive's native scale. If anything
    rescaled the domain onto 0–1, native 0.72 would map to 0.44 and land in
    `drop` instead of `review`."""
    assert band_hive("0.72")[0] == "review"


# ── Rules 6 & 7: categorical ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("full_match", "auto_confirm"),
        ("partial_match", "review"),
        ("page_match", "review"),
    ],
)
def test_categorical_bands_come_from_lookup(category: str, expected: str) -> None:
    d = band_for_attestation(google_entry(), "categorical", None, category)
    assert d.band == expected
    assert d.reason == f"categorical:{expected}"


def test_categorical_never_touches_provider_score() -> None:
    """Google Web Detection reports no number. provider_score stays NULL all
    the way through — inventing one would be normalising in the adapter."""
    d = band_for_attestation(google_entry(), "categorical", None, "full_match")
    assert d.band == "auto_confirm"
    # And a stray score alongside a categorical kind is ignored, not used.
    d2 = band_for_attestation(
        google_entry(), "categorical", Decimal("0.99"), "page_match"
    )
    assert d2.band == "review"


def test_unknown_category_is_review() -> None:
    d = band_for_attestation(google_entry(), "categorical", None, "some_new_kind")
    assert d.band == "review"
    assert d.reason == "unknown_category"


def test_calibration_version_is_stamped_on_a_real_decision() -> None:
    d = band_for_attestation(hive_entry(), "numeric", Decimal("0.99"), None)
    assert d.calibration_version == "hive-cal-v1"


# ── Review fixes: non-finite scores, unbounded domains, and the top-band ──
# ── inclusivity bug (CRITICAL C2, IMPORTANT I1 & I2) ──────────────────────

@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_non_finite_score_is_review_not_a_crash(bad: Decimal) -> None:
    """C2. provider_score is NUMERIC(6,4) and Postgres NUMERIC accepts NaN
    and the infinities; provider_score is an unvalidated Decimal. Comparing
    a non-finite value against band edges must not raise
    decimal.InvalidOperation, and it must be diagnosably different from an
    in-range-but-impossible value: its own reason, not score_out_of_domain.
    """
    d = band_for_attestation(hive_entry(), "numeric", bad, None)
    assert d.band == "review"
    assert d.reason == "score_not_finite"


def test_unbounded_score_domain_is_review_not_a_silent_pass() -> None:
    """I2 — the most important one. score_domain is nullable in the DB. A
    provider row with no domain recorded produces ScoreDomain() with every
    field None. Before this fix, `_in_domain` treated "no bounds" as
    "everything passes", so an impossible value like -5 would be read as a
    low score and silently banded `drop` — invisible forever, reached
    through a missing config row rather than through the score itself.
    """
    entry = PolicyEntry(
        provider_id=HIVE,
        calibrated=True,
        score_domain=ScoreDomain(),
        config=hive_config(),
    )
    d = band_for_attestation(entry, "numeric", Decimal("-5"), None)
    assert d.band == "review"
    assert d.reason == "score_domain_unknown"


def _gap_entry() -> PolicyEntry:
    """A deliberately gappy config: drop tops out at 0.72, auto_confirm
    covers 0.72–0.90, but the domain runs to 1.0. Nothing covers [0.90, 1.0)."""
    return PolicyEntry(
        provider_id=HIVE,
        calibrated=True,
        score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
        config=CalibrationConfig(
            config_id=uuid4(),
            provider_id=HIVE,
            version="gap-cal-v1",
            score_kind="numeric",
            numeric_bands=(
                NumericBand(band="drop", max=Decimal("0.72")),
                NumericBand(
                    band="auto_confirm", min=Decimal("0.72"), max=Decimal("0.90")
                ),
            ),
        ),
    )


def test_top_band_below_domain_ceiling_does_not_get_inclusive_max() -> None:
    """I1 regression — the reviewer's exact finding. The old `_numeric_band`
    granted inclusive-max to whichever configured band nothing else started
    above, regardless of where the domain actually ends. `auto_confirm`
    (min=0.72, max=0.90) was the highest-starting band, so 0.90 wrongly
    banded `auto_confirm` even though the domain runs to 1.0 and the stated
    convention is max-exclusive except for the band that owns the domain's
    actual top. A person would have been told, unreviewed, at a boundary the
    semantics make exclusive. It must now fall through to
    `no_band_covers_score` — this is also the only covering test for that
    reason string, which was previously unreachable.
    """
    d = band_for_attestation(_gap_entry(), "numeric", Decimal("0.90"), None)
    assert d.band == "review"
    assert d.reason == "no_band_covers_score"


def test_top_band_whose_max_equals_domain_ceiling_stays_inclusive() -> None:
    """Companion to the fix above: when the top band's max genuinely reaches
    the domain ceiling, the ceiling value must still land inside it — the
    fix must not have swung to always excluding the top band's max."""
    entry = PolicyEntry(
        provider_id=HIVE,
        calibrated=True,
        score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
        config=CalibrationConfig(
            config_id=uuid4(),
            provider_id=HIVE,
            version="explicit-max-cal-v1",
            score_kind="numeric",
            numeric_bands=(
                NumericBand(band="drop", max=Decimal("0.72")),
                NumericBand(
                    band="auto_confirm", min=Decimal("0.72"), max=Decimal("1.0")
                ),
            ),
        ),
    )
    d = band_for_attestation(entry, "numeric", Decimal("1.00"), None)
    assert d.band == "auto_confirm"


# ── Band JSON validation, used by `propose` ──────────────────────────────

def test_valid_bands_have_no_problems() -> None:
    assert validate_numeric_bands(HIVE_BANDS, HIVE_DOMAIN) == []


def test_boundary_outside_domain_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.20")),
        NumericBand(band="review", min=Decimal("0.20")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("outside score_domain" in p for p in problems)


def test_gap_in_coverage_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.70")),
        NumericBand(band="auto_confirm", min=Decimal("0.80")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("gap" in p for p in problems)


def test_overlap_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.80")),
        NumericBand(band="auto_confirm", min=Decimal("0.70")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("overlap" in p for p in problems)


def test_zero_width_band_is_rejected() -> None:
    """I4. drop(max=0.72) / review(0.72, 0.72) / auto_confirm(min=0.72) tiles
    the domain with no gap and no overlap by every other check here, but
    `review`'s min==max means it can never fire — `_numeric_band` is
    min-inclusive/max-exclusive, so nothing ever lands in [0.72, 0.72)."""
    bad = (
        NumericBand(band="drop", max=Decimal("0.72")),
        NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.72")),
        NumericBand(band="auto_confirm", min=Decimal("0.72")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("zero width" in p for p in problems)


def test_unbounded_domain_min_is_rejected_not_silently_valid() -> None:
    """I3. With domain.min unset, the coverage walk's cursor starts at None
    and the first band's gap check was silently skipped — bands (0.8–0.9,
    0.9–inf) against ScoreDomain(min=None, max=1.0) used to report no
    problems despite an obvious gap below 0.8."""
    bands = (
        NumericBand(band="review", min=Decimal("0.8"), max=Decimal("0.9")),
        NumericBand(band="auto_confirm", min=Decimal("0.9")),
    )
    domain = ScoreDomain(min=None, max=Decimal("1.0"))
    problems = validate_numeric_bands(bands, domain)
    assert any("unbounded" in p for p in problems)


def test_numeric_bands_against_categorical_only_domain_is_rejected() -> None:
    """I3. A categorical ScoreDomain (categories set, min/max both None) is
    nonsensical to validate numeric bands against — it used to report no
    problems for exactly that reason: both bounds None means the coverage
    walk never runs."""
    problems = validate_numeric_bands(HIVE_BANDS, GOOGLE_DOMAIN)
    assert any("unbounded" in p for p in problems)
